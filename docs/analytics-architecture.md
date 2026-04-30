# Forge Analytics Architecture

## Executive Summary

This document describes the analytics subsystem for forge-observability. It uses
[dlt (data load tool)](https://dlthub.com/docs/intro) to extract data from Langfuse,
Prometheus, GitHub, and JIRA, load it into an external datastore, transform it using [dbt (data build tool)](https://www.getdbt.com/blog/what-exactly-is-dbt)and expose it through a
FastAPI Analytics API.

Data is organized using a **Medallion Architecture**:

- **Bronze** — raw, per-source, denormalized tables loaded by dlt
- **Staging** — thin dbt views that select and rename bronze columns (no joins)
- **Silver** — cross-source joins built by dbt as datastore views
- **Gold** — pre-aggregated KPIs (not yet implemented)

Source systems remain the source of truth. The external datastore holds denormalized,
query-ready copies.

## Design Principles

1. **dlt as the ingestion backbone** — extraction, schema evolution, incremental
   loading, and datasource destination management are all handled by dlt
2. **dbt as the transformation layer** — silver/gold views are SQL models in a dbt
   project, rebuilt on a schedule by the worker process
3. **Graceful source degradation** — silver dbt models use `is_source_available()`
   to omit joins for bronze tables that haven't been loaded yet
4. **Source systems stay authoritative** — Langfuse, Prometheus, GitHub, JIRA
   are never replaced

## Architecture Overview

```
┌───────────────────────────────────────────────────────────────────┐
│                    Data Sources (Source of Truth)                 │
├────────────────┬────────────────┬────────────────┬────────────────┤
│    Langfuse    │   Prometheus   │   GitHub API   │    JIRA API    │
│  (LLM traces)  │  (app metrics) │   (PRs, CI)    │    (tickets)   │
└───────┬────────┴───────┬────────┴───────┬────────┴───────┬────────┘
        │                │                │                │
        │                │ dlt pipelines  │                │
        ▼                ▼                ▼                ▼
┌──────────────────────────────────────────────────────────────────┐
│  CONTAINER: forge-observability-worker                           │
│                                                                  │
│  dlt bronze pipelines (run concurrently via asyncio):            │
│    langfuse_pipeline   ──▶  bronze___llm_traces                  │
│    prometheus_pipeline ──▶  bronze___app_metrics                 │
│    github_pipeline     ──▶  bronze___pull_requests               │
│                             bronze___ci_checks                   │
│    jira_pipeline       ──▶  bronze___jira_tickets                │
│                             bronze___human_interactions          │
│                                                                  │
│  dbt (fires after each pipeline round via dlt.dbt.package):      │
│    staging models  ──▶  stg_llm_traces, stg_jira_tickets, …      │
│    silver models   ──▶  silver___ticket_full_view                │
│                         silver___ticket_llm_summary              │
│                         silver___pr_with_llm_cost                │
│                         silver___stage_performance               │
│                                                                  │
│  dlt manages: cursors, schema evolution, destination config      │
│  dbt manages: silver/gold view definitions, source availability  │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│  External Analytical Datastore (e.g, ClickHouse)                 │
│                                                                  │
│  bronze___*   Raw per-source tables (loaded by dlt)              │
│  silver___*   Cross-source views (built by dbt)                  │ 
│  gold___*     Pre-aggregated KPIs (built by dbt)                 │
│                                                                  │
│  Table naming: triple-underscore encodes logical schema prefix.  │
│  All tables live in one external datastore; no schema objects.   │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│  CONTAINER: forge-observability-api (:8010)                      │
│                                                                  │
│  FastAPI — routes query datastore directly via SQLAlchemy        │
│  Repository layer — SQLAlchemy Core, backend-agnostic reads      │
│                                                                  │
│  Aggregation queries  → silver___* views                         │
│  Drill-down queries   → bronze___* tables                        │
└──────────────────────────────────────────────────────────────────┘
```

**Container Breakdown:**

| Container | Replicas | Responsibility |
|-----------|----------|----------------|
| **forge-observability-worker** | 1 | Runs dlt pipelines. (1-N copies as dev goal)|
| **forge-observability-api** | 1-N | Serves HTTP API. Queries silver for aggregations, bronze for drill-downs. |

### Monitoring

- dlt exposes pipeline metrics (rows loaded, duration, errors) scrapeable by Prometheus
- The worker logs pipeline and dbt run results at INFO level

## Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **ELT** | dlt + dbt | Handles extraction, schema evolution, incremental loading, datastore destination |
| **Repository** | SQLAlchemy Core | Backend-agnostic read path; mockable in tests |
| **API** | Direct repository queries, no service layer | Queries are simple enough that a service layer adds no value |
| **Source degradation** | `is_source_available()` dbt macro | Silver/Gold views work with any subset of sources present |
