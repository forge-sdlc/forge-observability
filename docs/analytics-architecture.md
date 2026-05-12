# Forge Analytics Architecture

## Executive Summary

This document describes the analytics subsystem for forge-observability. It uses
[dlt (data load tool)](https://dlthub.com/docs/intro) to extract data from Langfuse
and load it into a target datastore, then transforms it using [dbt (data build tool)](https://www.getdbt.com/blog/what-exactly-is-dbt)
to build analytics views.

Data is organized using a **Medallion Architecture**:

- **Bronze** — raw, denormalized tables loaded by dlt
- **Silver** — analytical tables built by dbt as datastore views
- **Gold** — aggregated KPIs (placeholder, not yet implemented)

Source systems remain the source of truth. The external datastore holds denormalized,
query-ready copies.

## Design Principles

1. **dlt as the ingestion backbone** — extraction, schema evolution, incremental
   loading, and datastore destination management are all handled by dlt
2. **dbt as the transformation layer** — views are SQL models in a dbt
   project, rebuilt after each successful pipeline run
3. **Graceful source degradation** — dbt models use `is_source_available()` to
   enable themselves only when the corresponding bronze table exists in the datastore.
   The check uses `adapter.get_relation()` — dbt queries the live database at compile
   time rather than relying on external input
4. **Source systems stay authoritative** — Langfuse is never replaced

## Architecture Overview

```
┌───────────────────────────────────────────────────────────────────┐
│                    Data Sources (Source of Truth)                 │
├───────────────────────────────────────────────────────────────────┤
│                           Langfuse                                │
│                         (LLM traces)                              │
└───────────────────────────────┬───────────────────────────────────┘
                                │
                                │ dlt pipeline
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  CONTAINER: forge-observability-worker                           │
│                                                                  │
│  dlt bronze pipeline (runs on LANGFUSE_INTERVAL_SECONDS):        │
│    langfuse_pipeline  ──▶  bronze___llm_traces                   │
│                             bronze___llm_observations            │
│                             bronze___llm_scores                  │
│                                                                  │
│  dbt (fires after each successful pipeline run):                 │
│    staging models ──▶  stg_llm_traces                            │
│    silver models  ──▶  silver___stage_performance                │
│                         silver___ticket_llm_summary              │
│                                                                  │
│  dlt manages: cursors, schema evolution, destination config      │
│  dbt manages: view definitions, source availability              │
│                                                                  │
│  State persisted to /app/state/ (mounted volume):                │
│    .dlt/  — dlt pipeline state and cursors                       │
│    .dbt/  — dbt packages, target artifacts, logs                 │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│  External Analytical Datastore (ClickHouse)                      │
│                                                                  │
│  bronze___*   Raw per-source tables (loaded by dlt)              │
│  silver___*   Analytical tables (built by dbt)                   │
│  gold___*     aggregated KPIs (built by dbt, placeholder)        │
│                                                                  │
│  Table naming: triple-underscore encodes logical layer prefix.   │
│  All tables live in one database; no schema objects.             │
└──────────────────────────────────────────────────────────────────┘
```

## dbt Source Availability

The `is_source_available(source_name)` macro uses `adapter.get_relation()` to check
whether a bronze table physically exists in the datastore at dbt compile time:

```sql
{% macro is_source_available(source_name) %}
  {%- set relation = adapter.get_relation(
    database=source('bronze', source_name).database,
    schema=source('bronze', source_name).schema,
    identifier=source('bronze', source_name).identifier
  ) -%}
  {{ return(relation is not none) }}
{% endmacro %}
```

Models declare `enabled=is_source_available('llm_traces')` in their config block.
On a fresh datastore, all models are disabled until the first pipeline run populates
the bronze table. On subsequent dbt runs (triggered after each successful pipeline
load), the models enable themselves automatically.

`sources.yml` is the source of truth for which bronze tables exist and what their
physical identifiers are.

## Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **ELT** | dlt + dbt | Handles extraction, schema evolution, incremental loading, destination management |
| **dbt trigger** | After each successful pipeline run | Keeps views fresh without a separate scheduling mechanism |
| **Source availability** | `adapter.get_relation()` in dbt macro | dbt queries live DB state |
| **Multiple pipelines** | Langfuse + ??? | Architecture supports multiple concurrent asyncio pipelines; additional sources added by registering new pipeline factories in `worker.py` |
