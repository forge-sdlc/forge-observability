{{ config(
    materialized='view',
    alias='silver___stage_performance',
    enabled=is_source_available('llm_traces')
) }}

select
    t.workflow_stage                               as workflow_stage,
    COUNT(*)                                       as trace_count,
    sum(t.total_cost)                              as total_cost,
    avg(t.latency_ms)                              as avg_latency_ms,

    {% if is_source_available('human_interactions') %}
    coalesce(hi.total_approvals, 0)                as total_approvals,
    coalesce(hi.total_rejections, 0)               as total_rejections,
    case
        when coalesce(hi.total_approvals, 0) + coalesce(hi.total_rejections, 0) > 0
        then hi.total_approvals / (hi.total_approvals + hi.total_rejections)
        else null
    end                                            as approval_rate,
    {% endif %}

    1                                              as _placeholder

from {{ source('bronze', 'llm_traces') }} t

{% if is_source_available('human_interactions') %}
left join (
    select
        workflow_stage,
        SUM(CASE WHEN interaction_type = 'approval'  THEN 1 ELSE 0 END) as total_approvals,
        SUM(CASE WHEN interaction_type = 'rejection' THEN 1 ELSE 0 END) as total_rejections
    from {{ source('bronze', 'human_interactions') }}
    where workflow_stage != ''
    group by workflow_stage
) hi on t.workflow_stage = hi.workflow_stage
{% endif %}

where t.workflow_stage != ''
group by
    t.workflow_stage
    {% if is_source_available('human_interactions') %}
    , hi.total_approvals
    , hi.total_rejections
    {% endif %}
