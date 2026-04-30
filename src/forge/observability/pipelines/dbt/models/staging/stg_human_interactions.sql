{{ config(
    materialized='view',
    enabled=is_source_available('human_interactions')
) }}

select
    ticket_key,
    workflow_stage,
    interaction_type,
    interacted_at
from {{ source('bronze', 'human_interactions') }}
