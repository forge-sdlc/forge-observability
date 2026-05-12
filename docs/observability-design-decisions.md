# Observability Design Decisions

## Summary

This document captures the architectural decisions agreed upon between Ella Shulman and Dan Childers regarding the observability strategy for the Forge SDLC Orchestrator. It covers two distinct concerns: **system observability** (Grafana dashboards with drill-down capability) and **ticket-level reporting** (Jira summaries posted by Forge directly).

---

## Architecture Overview

The observability system splits into two independent concerns with a clear boundary:

```
┌──────────────────────────────────────────────────────────────────────┐
│  System Observability (forge-observability)                          │
│                                                                      │
│  Prometheus ─────────────────────────────────► Grafana              │
│    (metrics + exemplars with trace_id)            ↑                 │
│                                                   │                 │
│  Langfuse ClickHouse (self-hosted, with MVs) ─────┘                 │
│    (LLM traces, agent observations)               │                 │
│                                                   │                 │
│  FastMCP server ──────────────────────────────────┘                 │
│    query_langfuse_traces / query_prometheus_metrics                  │
│    → used by observability:generate-report + improve-skills          │
│                                                                      │
│  Anomaly detected in Grafana → click exemplar → Langfuse trace      │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│  Ticket Reporting (forge)                                            │
│                                                                      │
│  Forge workflow completes → read LangGraph state →                  │
│  post summary comment to Jira ticket                                │
└──────────────────────────────────────────────────────────────────────┘
```

**Key principle**: Observability = system-wide view with deep-dive capability via Grafana. Jira info = per-ticket run summary posted immediately from Forge state when the run completes.

---

## Part 1: System Observability

### Decision: Prometheus + Langfuse ClickHouse as dual Grafana data sources

Grafana connects directly to both Prometheus and Langfuse's ClickHouse instance. The medallion/analytical logic lives in Grafana and (later) in the MCP API, not in an external ETL pipeline for this use case.

**Rationale**: Querying Langfuse's ClickHouse directly is viable because ClickHouse is an OLAP columnar store built for analytical queries under concurrent load. For the current scale (≤150 projects), a second ETL pipeline adds infrastructure overhead without meaningful performance benefit.

### Prometheus: Metrics and Recording Rules

Forge emits counters and histograms to Prometheus for macro-level anomaly detection.

**Cardinality management**: Projects are long-running (dozens active at any time), so `project_id` is safe as a Prometheus label. Total series: ~2 workflows × ~15 stages × ~50 projects = ~1,500 active series—well within Prometheus's operational range with no churn risk.

**Recording Rules** are still recommended for dashboard performance, aggregating per stage for long-term storage and fast Grafana load times:

```yaml
groups:
  - name: forge_sdlc_metrics
    rules:
      - record: job:app_revisions:rate5m
        expr: sum by (workflow, stage) (rate(app_revisions_total[5m]))

      - record: job:app_revisions:anomaly_score
        expr: >
          job:app_revisions:rate5m
          > 1.5 * avg_over_time(job:app_revisions:rate5m[1h])
```

Grafana dashboards query the pre-aggregated recording rules for instant load times. The raw `project_id`-labeled series remain available for per-project drill-down queries.

### Prometheus Exemplars: Linking Metrics to Langfuse Traces

**Exemplars** attach a `trace_id` to individual metric increments, creating a navigable link from a Prometheus spike to the exact Langfuse trace.

When Forge increments a counter, it injects the current Langfuse `trace_id` as an exemplar:

```
app_revisions_total{workflow="A", stage="code_generation", project_id="spark-102"} 1
  # {trace_id="lf-a1b2c3d4"}
```

In Grafana, this renders as diamond markers on the time series graph. Clicking a spike's diamond navigates directly to the corresponding Langfuse trace—eliminating manual timestamp correlation.

`trace_id` is the join key between Prometheus and Langfuse data. This is the primary cross-reference mechanism, not a foreign-key relationship in a shared database.

### Langfuse ClickHouse: Materialized Views

Langfuse is **self-hosted** — we control the ClickHouse instance. This makes MVs fully feasible without any additional infrastructure.

To avoid coupling Grafana dashboards to Langfuse's internal schema and to make analytical queries faster, **ClickHouse Materialized Views (MVs)** are created within the existing Langfuse ClickHouse instance.

MVs fire on insert into the Langfuse `observations` table, aggregating relevant fields into a custom table owned by forge-observability:

| Table | Purpose |
|-------|---------|
| `langfuse.observations` | Langfuse-managed raw data (source of truth) |
| `forge.agent_performance_rollup` | MV target: pre-aggregated by `project_id`, `workflow_stage`, `latency`, `token_count` |

**Benefits**:
- Zero additional infrastructure (runs inside the existing ClickHouse instance)
- Grafana queries the small pre-aggregated table, not raw trace scans
- Because both tables share the same DB, `JOIN` back to raw traces by `trace_id` remains available for drill-down

**Risk**: If the Langfuse schema changes, the MV definition may need updating. This is acceptable given that querying raw tables directly has the same risk.

### Grafana: Deep Links to Langfuse Traces

Grafana dashboards include deep links from anomaly views to the specific Langfuse trace. This can be implemented via:
- Exemplar click-through (automatic when exemplars are configured)
- Grafana data link using the `trace_id` field from the ClickHouse MV query, linking to Langfuse's trace URL

---

## Part 2: Ticket-Level Reporting (Jira)

### Decision: Forge posts Jira summaries directly from workflow state

Jira ticket summaries are **not** sourced from forge-observability or an API. Forge itself posts a comment to the Jira ticket at the end of each workflow run, reading directly from the LangGraph checkpoint state.

**Rationale**: The data needed for the summary (token counts, stage durations, revision counts, final outcome) is already present in Forge's LangGraph state and checkpointing mechanism. Routing this through an external API would add latency and coupling without benefit. The summary is immediate and ticket-scoped.

### Required additions to Forge

#### State additions

The LangGraph workflow state needs to carry observability fields throughout execution so the final node can assemble the summary without re-querying Langfuse:

```python
# In forge/models/workflow.py (or equivalent state model)
class WorkflowState(TypedDict):
    # ... existing fields ...
    observability: ObservabilityState

class ObservabilityState(TypedDict):
    langfuse_trace_id: str          # set at workflow start
    stage_durations_s: dict[str, float]   # stage → elapsed seconds
    revision_counts: dict[str, int]       # stage → retry count
    total_input_tokens: int
    total_output_tokens: int
    final_outcome: str              # "completed" | "blocked" | "failed"
```

#### Jira comment node

A terminal LangGraph node (or post-processing hook on workflow completion) reads the `observability` slice of state and posts a structured comment to the Jira ticket:

```
*Forge Run Summary*
• Outcome: completed
• Total duration: 4m 32s
• Stages: prd (45s) → spec (1m 12s) → plan (38s) → implement (2m 17s)
• Revisions: implement ×3
• Tokens: 12,450 in / 8,930 out
• Trace: [View in Langfuse|https://langfuse.example.com/traces/lf-a1b2c3d4]
```

The Langfuse trace deep link is derived from `langfuse_trace_id` in state and the configured `LANGFUSE_HOST`.

#### When to post

The summary is posted on every terminal state transition:
- Workflow completes successfully (all stages done, PR merged)
- Workflow is blocked (label `forge:blocked` applied)
- Workflow fails unrecoverably

### MCP API: Observability Query Surface

The forge-observability MCP API is **in scope** (tracked as [issue #2](https://github.com/forge-sdlc/forge-observability/issues/2): *Post-evaluation agent: MCP server and skills for skill improvement from observability data*).

#### Purpose

Give agents read access to observability data so they can analyze how skills performed during a Forge run and collaboratively improve them. This enables a feedback loop: Forge runs → observability data → skill analysis → targeted improvements → better future runs.

#### MCP server (data access layer)

A [FastMCP](https://github.com/jlowin/fastmcp) server with two raw query tools and no business logic:

| Tool | Data source | Description |
|------|------------|-------------|
| `query_langfuse_traces` | Langfuse ClickHouse MVs | LLM trace data (prompts, completions, latency, token usage), filterable by run ID, time range, or skill name |
| `query_prometheus_metrics` | Prometheus | Forge-emitted metrics (stage durations, human interaction outcomes, PR stats), filterable by metric name, time range, and labels |

All interpretation happens in skills, not in the server. The server is intentionally thin.

#### Skills

**`observability:generate-report`** — fetches traces and metrics for a given Forge run or time window and produces a structured report:
- Which skills were invoked and how often
- Token usage and latency per skill
- Stage durations and bottlenecks
- Human interaction rates and outcomes

**`observability:improve-skills`** — reads the report and the team's skill files (from their GitHub repo), then:
- Identifies skills that underperformed or caused friction based on the data
- Generates targeted improvement suggestions one at a time, showing the proposed diff
- Asks the user whether to apply each change before making any edits (fully human-in-the-loop)
- Applies approved changes directly to the skill files in the repo

#### Design constraints

- Skill files are team-specific and live in the team's GitHub repository; the improvement skill reads and writes them there
- No Forge-specific aggregations baked into the MCP server — that stays in the skills and reports
- The MCP server queries the same ClickHouse MV tables and Prometheus recording rules used by Grafana, so no additional data pipeline is needed

---

## Required Changes

### forge-observability

| Change | Description |
|--------|-------------|
| Prometheus instrumentation | Emit counters/histograms with `workflow` and `stage` labels from the worker or via Forge push |
| Exemplar injection | Attach `trace_id` to metric increments when a Langfuse trace is active |
| ClickHouse MV definitions | Create and maintain `forge.agent_performance_rollup` MV on the Langfuse ClickHouse instance |
| Grafana provisioning | Dashboard definitions with Prometheus + ClickHouse data sources, exemplar click-through, and Langfuse deep links |
| Prometheus Recording Rules | Ship rule definitions alongside the worker for deployment |
| FastMCP server | `query_langfuse_traces` and `query_prometheus_metrics` tools backed by ClickHouse MVs and Prometheus respectively |
| `observability:generate-report` skill | Fetches traces + metrics for a run/time window and produces structured performance report |
| `observability:improve-skills` skill | Reads report + team skill files, proposes diffs one-at-a-time, applies user-approved changes to the skill repo |

### forge

| Change | Description |
|--------|-------------|
| `ObservabilityState` model | Add observability tracking fields to LangGraph state |
| State population | Instrument each workflow stage node to write duration, revision counts, and token totals into state |
| `langfuse_trace_id` propagation | Capture the trace ID at workflow start, store in state |
| Jira summary node | Terminal node that reads observability state and posts structured comment to the Jira ticket via the existing Jira integration |
| Deep link construction | Compose Langfuse trace URL from `LANGFUSE_HOST` and `langfuse_trace_id` |
