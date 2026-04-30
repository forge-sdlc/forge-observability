{{ config(
    materialized='view',
    alias='silver___ticket_full_view',
    enabled=is_source_available('jira_tickets')
) }}

select
    j.ticket_key                                         as ticket_key,
    j.ticket_type                                        as ticket_type,
    j.status                                             as ticket_status,
    j.forge_labels                                       as forge_labels,
    j.created_at                                         as ticket_created_at,

    {% if is_source_available('llm_traces') %}
    coalesce(llm.total_cost, 0)                          as llm_total_cost,
    coalesce(llm.avg_latency_ms, 0)                      as llm_avg_latency_ms,
    llm.first_trace_at                                   as first_trace_at,
    llm.last_trace_at                                    as last_trace_at,
    coalesce(llm.trace_count, 0)                         as llm_trace_count,
    {% endif %}

    {% if is_source_available('pull_requests') %}
    coalesce(pr.pr_count, 0)                             as pr_count,
    coalesce(pr.prs_merged, 0)                           as prs_merged,
    coalesce(pr.lines_added, 0)                          as total_lines_added,
    coalesce(pr.lines_deleted, 0)                        as total_lines_deleted,
    {% endif %}

    {% if is_source_available('human_interactions') %}
    coalesce(hi.total_approvals, 0)                      as total_approvals,
    coalesce(hi.total_rejections, 0)                     as total_rejections,
    coalesce(hi.total_questions, 0)                      as total_questions,
    {% endif %}

    1                                                    as _placeholder

from {{ source('bronze', 'jira_tickets') }} j

{% if is_source_available('llm_traces') %}
left join (
    select
        ticket_key,
        COUNT(*)           as trace_count,
        sum(total_cost)    as total_cost,
        avg(latency_ms)    as avg_latency_ms,
        min(timestamp)     as first_trace_at,
        max(timestamp)     as last_trace_at
    from {{ source('bronze', 'llm_traces') }}
    where ticket_key != ''
    group by ticket_key
) llm on j.ticket_key = llm.ticket_key
{% endif %}

{% if is_source_available('pull_requests') %}
left join (
    select
        ticket_key,
        COUNT(*)                                                       as pr_count,
        SUM(CASE WHEN merged = true THEN 1 ELSE 0 END)                as prs_merged,
        sum(coalesce(lines_added, 0))                                  as lines_added,
        sum(coalesce(lines_deleted, 0))                                as lines_deleted
    from {{ source('bronze', 'pull_requests') }}
    where ticket_key != ''
    group by ticket_key
) pr on j.ticket_key = pr.ticket_key
{% endif %}

{% if is_source_available('human_interactions') %}
left join (
    select
        ticket_key,
        SUM(CASE WHEN interaction_type = 'approval'  THEN 1 ELSE 0 END) as total_approvals,
        SUM(CASE WHEN interaction_type = 'rejection' THEN 1 ELSE 0 END) as total_rejections,
        SUM(CASE WHEN interaction_type = 'question'  THEN 1 ELSE 0 END) as total_questions
    from {{ source('bronze', 'human_interactions') }}
    group by ticket_key
) hi on j.ticket_key = hi.ticket_key
{% endif %}
