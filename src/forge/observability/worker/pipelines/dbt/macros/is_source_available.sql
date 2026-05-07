{% macro is_source_available(source_name) %}
  {# Returns true when source_name appears in the available_sources runtime variable. #}
  {# adapter.get_relation() cannot be used here: config(enabled=...) is evaluated at  #}
  {# parse time before a database connection exists, so it would always return None.   #}
  {% set available = var('available_sources', []) %}
  {{ return(source_name in available) }}
{% endmacro %}
