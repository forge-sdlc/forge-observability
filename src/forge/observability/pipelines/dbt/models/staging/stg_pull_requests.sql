{{ config(
    materialized='view',
    enabled=is_source_available('pull_requests')
) }}

select
    repo,
    pr_number,
    ticket_key,
    title,
    state,
    merged,
    author,
    lines_added,
    lines_deleted,
    files_changed,
    review_comments,
    created_at
from {{ source('bronze', 'pull_requests') }}
