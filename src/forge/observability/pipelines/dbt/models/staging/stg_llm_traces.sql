{{ config(
    materialized='view',
    enabled=is_source_available('llm_traces')
) }}

select
    trace_id,
    name,
    ticket_key,
    workflow_stage,
    latency_ms,
    total_cost,
    timestamp
from {{ source('bronze', 'llm_traces') }}
