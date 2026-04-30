{{ config(
    materialized='view',
    enabled=is_source_available('ci_checks')
) }}

select
    repo,
    check_run_id,
    name,
    status,
    conclusion,
    started_at,
    completed_at
from {{ source('bronze', 'ci_checks') }}
