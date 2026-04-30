{{ config(
    materialized='view',
    alias='silver___pr_with_llm_cost',
    enabled=is_source_available('pull_requests')
) }}

select
    p.repo,
    p.pr_number,
    p.ticket_key,
    p.title,
    p.state,
    p.merged,
    p.author,
    p.created_at,
    coalesce(p.lines_added, 0)      as lines_added,
    coalesce(p.lines_deleted, 0)    as lines_deleted,
    coalesce(p.files_changed, 0)    as files_changed,
    coalesce(p.review_comments, 0)  as review_comments,

    {% if is_source_available('llm_traces') %}
    coalesce(llm.total_cost, 0)     as llm_total_cost,
    coalesce(llm.trace_count, 0)    as llm_trace_count,
    case
        when coalesce(p.lines_added, 0) > 0 and coalesce(llm.total_cost, 0) > 0
        then llm.total_cost / p.lines_added
        else 0
    end                             as llm_cost_per_line,
    {% endif %}

    1                               as _placeholder

from {{ source('bronze', 'pull_requests') }} p

{% if is_source_available('llm_traces') %}
left join (
    select
        ticket_key,
        sum(total_cost)  as total_cost,
        COUNT(*)         as trace_count
    from {{ source('bronze', 'llm_traces') }}
    where ticket_key != ''
    group by ticket_key
) llm on p.ticket_key = llm.ticket_key
{% endif %}
