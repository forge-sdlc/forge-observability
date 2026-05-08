"""Tests for forge.observability.worker.pipelines."""

import inspect
from unittest.mock import MagicMock

import pytest

from forge.observability.worker.pipelines.langfuse_pipeline import (
    _fetch_pages,
    _yield_observations,
    _yield_scores,
    _yield_traces,
)

# ── Lag wiring ─────────────────────────────────────────────────────────────


class TestLangfuseLagWiring:
    """Verify that lag_seconds flows from Settings through the worker into each dlt resource."""

    def test_lag_seconds_passed_from_settings(self, monkeypatch):
        """_make_langfuse_source passes lag_seconds from settings to langfuse_source."""
        from pydantic import SecretStr

        from forge.observability.config import Settings
        from forge.observability.worker import worker

        captured = {}

        def fake_langfuse_source(**kwargs):
            captured.update(kwargs)

        s = Settings(
            _env_file=None,
            langfuse_public_key="pk",
            langfuse_secret_key=SecretStr("sk"),
            langfuse_collection_lag_seconds=600,
        )
        monkeypatch.setattr(worker, "get_settings", lambda: s)
        monkeypatch.setattr(
            "forge.observability.worker.pipelines.langfuse_pipeline.langfuse_source",
            fake_langfuse_source,
        )

        worker._make_langfuse_source()

        assert captured["lag_seconds"] == 600
        assert captured["page_size"] == s.langfuse_page_size

    @pytest.mark.parametrize(
        "resource_name,cursor_param",
        [
            ("traces", "updated_at"),
            ("observations", "event_ts"),
            ("scores", "event_ts"),
        ],
    )
    def test_each_resource_uses_lag(self, resource_name, cursor_param):
        """Each langfuse resource captures lag_seconds in its incremental cursor."""
        from forge.observability.worker.pipelines.langfuse_pipeline import langfuse_source

        source = langfuse_source(
            host="http://h", public_key="pk", secret_key="sk", lag_seconds=999, page_size=10
        )
        resource = source.resources[resource_name]
        sig = inspect.signature(resource._pipe.gen)
        assert sig.parameters[cursor_param].default.lag == 999


# ── Cursor / API filter alignment ──────────────────────────────────────────


class TestLangfuseCursorApiAlignment:
    """Each resource must use a consistent (cursor field, API filter key) pair.

    The cursor field is what dlt uses to advance its incremental state; the API
    filter key is what gets sent to Langfuse so the server applies the same
    boundary.  If these diverge the pipeline silently re-fetches the wrong
    window — or drops records entirely.

    Authoritative pairs:
      traces       → updated_at  /  fromUpdatedAt   (API supports updatedAt filter)
      observations → event_ts    /  fromStartTime   (API only exposes startTime)
      scores       → event_ts    /  fromTimestamp   (API only exposes timestamp)
    """

    @pytest.mark.parametrize(
        "resource_name,cursor_field,api_filter_key",
        [
            ("traces", "updated_at", "fromUpdatedAt"),
            ("observations", "event_ts", "fromStartTime"),
            ("scores", "event_ts", "fromTimestamp"),
        ],
    )
    def test_cursor_field_and_api_filter_key(
        self, monkeypatch, resource_name, cursor_field, api_filter_key
    ):
        import forge.observability.worker.pipelines.langfuse_pipeline as pipeline_module
        from forge.observability.worker.pipelines.langfuse_pipeline import langfuse_source

        # Suppress real HTTP client creation
        monkeypatch.setattr(pipeline_module, "_langfuse_client", lambda *_, **__: MagicMock())

        # Mock _fetch_pages to capture the params dict without making HTTP requests.
        # Params are passed as a plain positional argument so call_args.args is
        # reliable regardless of how dlt processes the generator's parameters.
        mock_fetch = MagicMock(return_value=iter([[]]))
        monkeypatch.setattr(pipeline_module, "_fetch_pages", mock_fetch)

        source = langfuse_source(
            host="http://h", public_key="pk", secret_key="sk", lag_seconds=0, page_size=10
        )
        resource = source.resources[resource_name]

        # Verify the cursor field declared in the function signature
        sig = inspect.signature(resource._pipe.gen)
        assert cursor_field in sig.parameters, (
            f"{resource_name}: expected cursor field '{cursor_field}', got {list(sig.parameters)}"
        )

        # dlt uses the passed argument directly as start_value on the incremental
        # object it injects into the generator, so pass the value itself
        list(resource._pipe.gen(**{cursor_field: "2024-06-01T00:00:00Z"}))

        assert mock_fetch.called, f"{resource_name}: _fetch_pages was never called"
        # _fetch_pages(client, path, params) — params is the third positional arg
        sent_params = mock_fetch.call_args.args[2]
        assert api_filter_key in sent_params, (
            f"{resource_name}: expected API filter key '{api_filter_key}', got {list(sent_params)}"
        )
        assert sent_params[api_filter_key] == "2024-06-01T00:00:00Z"


# ── _yield_traces ──────────────────────────────────────────────────────────


class TestYieldTraces:
    """Field mapping and type coercion for the llm_traces bronze table."""

    def test_basic_field_mapping(self):
        trace = {
            "id": "t1",
            "name": "plan",
            "latency": 1.5,
            "totalCost": 0.01,
            "sessionId": "sess-1",
            "userId": "user-1",
            "environment": "prod",
            "htmlPath": "/traces/t1",
            "timestamp": "2024-01-01T00:00:00Z",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-01T00:00:01Z",
        }
        result = list(_yield_traces([trace]))
        assert len(result) == 1
        row = result[0]
        assert row["trace_id"] == "t1"
        assert row["name"] == "plan"
        assert row["latency_ms"] == 1500
        assert row["total_cost"] == 0.01
        assert row["session_id"] == "sess-1"
        assert row["user_id"] == "user-1"
        assert row["environment"] == "prod"

    def test_latency_converts_seconds_to_ms(self):
        result = list(_yield_traces([{"id": "t1", "latency": 2.75}]))
        assert result[0]["latency_ms"] == 2750

    def test_null_latency_defaults_to_zero(self):
        result = list(_yield_traces([{"id": "t1", "latency": None}]))
        assert result[0]["latency_ms"] == 0

    def test_missing_optional_fields_default_to_empty_string(self):
        result = list(_yield_traces([{"id": "t1"}]))
        row = result[0]
        assert row["session_id"] == ""
        assert row["name"] == ""
        assert row["environment"] == ""
        assert row["user_id"] == ""
        assert row["html_path"] == ""

    def test_yields_one_row_per_trace(self):
        traces = [{"id": f"t{i}"} for i in range(4)]
        assert len(list(_yield_traces(traces))) == 4

    def test_empty_input_yields_nothing(self):
        assert list(_yield_traces([])) == []


# ── _yield_observations ────────────────────────────────────────────────────


class TestYieldObservations:
    """Field mapping and type coercion for the llm_observations bronze table."""

    def test_basic_field_mapping(self):
        obs = {
            "id": "o1",
            "traceId": "t1",
            "parentObservationId": "o0",
            "type": "GENERATION",
            "name": "call-llm",
            "model": "claude-3-5-sonnet",
            "level": "DEFAULT",
            "statusMessage": "",
            "latency": 2.0,
            "timeToFirstToken": 0.5,
            "usageDetails": {"input": 100, "output": 50, "total": 150},
            "totalCost": 0.02,
            "startTime": "2024-01-01T00:00:00Z",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-01T00:00:02Z",
        }
        result = list(_yield_observations([obs]))
        row = result[0]
        assert row["observation_id"] == "o1"
        assert row["trace_id"] == "t1"
        assert row["model"] == "claude-3-5-sonnet"
        assert row["latency_ms"] == 2000
        assert row["time_to_first_token_ms"] == 500
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50
        assert row["total_tokens"] == 150

    def test_null_latency_defaults_to_zero(self):
        result = list(_yield_observations([{"id": "o1", "latency": None}]))
        assert result[0]["latency_ms"] == 0

    def test_null_ttft_defaults_to_zero(self):
        result = list(_yield_observations([{"id": "o1", "timeToFirstToken": None}]))
        assert result[0]["time_to_first_token_ms"] == 0

    def test_missing_usage_defaults_to_zero(self):
        result = list(_yield_observations([{"id": "o1"}]))
        row = result[0]
        assert row["input_tokens"] == 0
        assert row["output_tokens"] == 0
        assert row["total_tokens"] == 0

    def test_missing_optional_string_fields_default_to_empty(self):
        result = list(_yield_observations([{"id": "o1"}]))
        row = result[0]
        assert row["trace_id"] == ""
        assert row["parent_observation_id"] == ""
        assert row["type"] == ""
        assert row["model"] == ""

    def test_yields_one_row_per_observation(self):
        obs = [{"id": f"o{i}"} for i in range(3)]
        assert len(list(_yield_observations(obs))) == 3


# ── _yield_scores ──────────────────────────────────────────────────────────


class TestYieldScores:
    """Field mapping and type coercion for the llm_scores bronze table."""

    def test_basic_field_mapping(self):
        score = {
            "id": "s1",
            "traceId": "t1",
            "observationId": "o1",
            "name": "quality",
            "dataType": "NUMERIC",
            "value": 0.9,
            "stringValue": "",
            "source": "HUMAN",
            "authorUserId": "user-1",
            "comment": "looks good",
            "timestamp": "2024-01-01T00:00:00Z",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-01T00:00:01Z",
        }
        result = list(_yield_scores([score]))
        row = result[0]
        assert row["score_id"] == "s1"
        assert row["trace_id"] == "t1"
        assert row["observation_id"] == "o1"
        assert row["name"] == "quality"
        assert row["value"] == 0.9
        assert row["source"] == "HUMAN"
        assert row["comment"] == "looks good"

    def test_missing_optional_fields_default_to_empty(self):
        result = list(_yield_scores([{"id": "s1"}]))
        row = result[0]
        assert row["trace_id"] == ""
        assert row["observation_id"] == ""
        assert row["string_value"] == ""
        assert row["comment"] == ""
        assert row["author_user_id"] == ""

    def test_null_value_is_preserved(self):
        result = list(_yield_scores([{"id": "s1", "value": None}]))
        assert result[0]["value"] is None

    def test_yields_one_row_per_score(self):
        scores = [{"id": f"s{i}"} for i in range(5)]
        assert len(list(_yield_scores(scores))) == 5


# ── _fetch_pages ───────────────────────────────────────────────────────────


def _make_response(data: list, total_pages: int) -> MagicMock:
    """Build a mock httpx response matching the Langfuse paginated envelope."""
    r = MagicMock()
    r.json.return_value = {"data": data, "meta": {"totalPages": total_pages}}
    return r


class TestFetchPages:
    """Pagination logic: page injection, totalPages boundary, and error propagation."""

    def test_single_page_stops_after_one_request(self):
        client = MagicMock()
        client.get.return_value = _make_response([{"id": "1"}], total_pages=1)
        pages = list(_fetch_pages(client, "/api/traces", {}))
        assert pages == [[{"id": "1"}]]
        assert client.get.call_count == 1

    def test_multiple_pages_fetches_all(self):
        client = MagicMock()
        client.get.side_effect = [
            _make_response([{"id": "1"}], total_pages=2),
            _make_response([{"id": "2"}], total_pages=2),
        ]
        pages = list(_fetch_pages(client, "/api/traces", {}))
        assert pages == [[{"id": "1"}], [{"id": "2"}]]
        assert client.get.call_count == 2

    def test_page_number_is_injected_into_params(self):
        client = MagicMock()
        client.get.return_value = _make_response([], total_pages=1)
        list(_fetch_pages(client, "/path", {"limit": 10}))
        client.get.assert_called_once_with("/path", params={"limit": 10, "page": 1})

    def test_empty_data_page_is_yielded(self):
        client = MagicMock()
        client.get.return_value = _make_response([], total_pages=1)
        pages = list(_fetch_pages(client, "/path", {}))
        assert pages == [[]]

    def test_raise_for_status_is_called(self):
        client = MagicMock()
        client.get.return_value = _make_response([], total_pages=1)
        list(_fetch_pages(client, "/path", {}))
        client.get.return_value.raise_for_status.assert_called_once()
