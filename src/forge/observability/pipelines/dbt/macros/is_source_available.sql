{% macro is_source_available(source_name) %}
  {# Returns true when source_name appears in the available_sources runtime variable. #}
  {% set available = var('available_sources', []) %}
  {{ return(source_name in available) }}
{% endmacro %}
