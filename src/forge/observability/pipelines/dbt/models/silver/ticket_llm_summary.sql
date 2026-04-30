{{ config(
    materialized='view',
    alias='silver___ticket_llm_summary',
    enabled=is_source_available('llm_traces')
) }}

select
    ticket_key,
    workflow_stage,
    COUNT(*)               as trace_count,
    sum(latency_ms)        as total_latency_ms,
    avg(latency_ms)        as avg_latency_ms,
    sum(total_cost)        as total_cost,
    min(timestamp)         as first_trace_at,
    max(timestamp)         as last_trace_at

from {{ source('bronze', 'llm_traces') }}
where ticket_key != ''
group by ticket_key, workflow_stage
