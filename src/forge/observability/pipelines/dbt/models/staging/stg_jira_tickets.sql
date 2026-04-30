{{ config(
    materialized='view',
    enabled=is_source_available('jira_tickets')
) }}

select
    ticket_key,
    ticket_type,
    status,
    forge_labels,
    created_at
from {{ source('bronze', 'jira_tickets') }}
