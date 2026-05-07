{{ config(
    materialized='view',
    alias='silver___stage_performance',
    enabled=is_source_available('llm_traces')
) }}

select
    workflow_stage,
    COUNT(*)       as trace_count,
    sum(total_cost)  as total_cost,
    avg(latency_ms)  as avg_latency_ms

from {{ source('bronze', 'llm_traces') }}

where workflow_stage != ''
group by workflow_stage
