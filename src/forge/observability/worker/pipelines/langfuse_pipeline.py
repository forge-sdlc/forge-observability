"""dlt pipeline: Langfuse → bronze.llm_traces + bronze.llm_observations + bronze.llm_scores."""

import base64
from collections.abc import Iterator
from itertools import count

import dlt
import httpx
from dlt.sources import DltResource


def _langfuse_client(host: str, public_key: str, secret_key: str) -> httpx.Client:
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return httpx.Client(
        base_url=host.rstrip("/"),
        headers={"Authorization": f"Basic {token}"},
        timeout=30,
    )


def _fetch_pages(client: httpx.Client, path: str, params: dict) -> Iterator[list[dict]]:
    """Yield one page of results at a time until totalPages is reached.

    Callers are responsible for including `limit` in params. The `page`
    param is injected here and must not be set by the caller.
    """
    for page in count(1):
        r = client.get(path, params={**params, "page": page})
        r.raise_for_status()
        data = r.json()
        yield data.get("data", [])
        if page >= data.get("meta", {}).get("totalPages", 1):
            break


def _yield_traces(data: list[dict]) -> Iterator[dict]:
    for trace in data:
        latency = trace.get("latency")

        yield {
            "trace_id": trace["id"],
            "session_id": trace.get("sessionId") or "",
            "name": trace.get("name") or "",
            "latency_ms": int(latency * 1000) if latency else 0,
            "total_cost": trace.get("totalCost") or 0.0,
            "environment": trace.get("environment") or "",
            "html_path": trace.get("htmlPath") or "",
            "user_id": trace.get("userId") or "",
            "event_ts": trace.get("timestamp"),
            "created_at": trace.get("createdAt"),
            "updated_at": trace.get("updatedAt"),
        }


def _yield_observations(data: list[dict]) -> Iterator[dict]:
    for obs in data:
        usage = obs.get("usageDetails") or {}
        latency = obs.get("latency")
        ttft = obs.get("timeToFirstToken")

        yield {
            "observation_id": obs["id"],
            "trace_id": obs.get("traceId") or "",
            "parent_observation_id": obs.get("parentObservationId") or "",
            "type": obs.get("type") or "",
            "name": obs.get("name") or "",
            "model": obs.get("model") or "",
            "level": obs.get("level") or "",
            "status_message": obs.get("statusMessage") or "",
            "latency_ms": int(latency * 1000) if latency else 0,
            "time_to_first_token_ms": int(ttft * 1000) if ttft else 0,
            "input_tokens": usage.get("input", 0),
            "output_tokens": usage.get("output", 0),
            "total_tokens": usage.get("total", 0),
            "total_cost": obs.get("totalCost") or 0.0,
            "event_ts": obs.get("startTime"),
            "created_at": obs.get("createdAt"),
            "updated_at": obs.get("updatedAt"),
        }


def _yield_scores(data: list[dict]) -> Iterator[dict]:
    for score in data:
        yield {
            "score_id": score["id"],
            "trace_id": score.get("traceId") or "",
            "observation_id": score.get("observationId") or "",
            "name": score.get("name") or "",
            "data_type": score.get("dataType") or "",
            "value": float(v) if (v := score.get("value")) is not None else None,
            "string_value": score.get("stringValue") or "",
            "source": score.get("source") or "",
            "author_user_id": score.get("authorUserId") or "",
            "comment": score.get("comment") or "",
            "event_ts": score.get("timestamp"),
            "created_at": score.get("createdAt"),
            "updated_at": score.get("updatedAt"),
        }


@dlt.source(name="langfuse")
def langfuse_source(
    host: str,
    public_key: str,
    secret_key: str,
    lag_seconds: int,
    page_size: int,
) -> DltResource:
    """Extract LLM traces, observations, and scores from Langfuse.

    Each resource uses `dlt.sources.incremental` to track the `updated_at`
    cursor across runs. `lag_seconds` causes dlt to re-fetch records updated
    within that window on every run, providing a buffer for Langfuse's
    eventual-consistency writes (e.g. totalCost is computed asynchronously
    and may arrive after the trace is first created).

    The Langfuse API uses different filter parameter names per endpoint:
      - traces      → fromUpdatedAt
      - observations → fromStartTime  (no updatedAt filter on this endpoint)
      - scores      → fromTimestamp
    """

    @dlt.resource(
        table_name="llm_traces",
        write_disposition="merge",
        primary_key="trace_id",
    )
    def traces(
        # noqa: B008 — dlt requires a function-call default to wire the incremental state
        updated_at: dlt.sources.incremental[str] = dlt.sources.incremental(  # noqa: B008
            "updated_at",
            initial_value=None,
            lag=lag_seconds,
        ),
    ) -> Iterator[dict]:
        with _langfuse_client(host, public_key, secret_key) as client:
            params: dict = {"limit": page_size}
            if updated_at.start_value:
                params["fromUpdatedAt"] = updated_at.start_value
            for page_data in _fetch_pages(client, "/api/public/traces", params):
                yield from _yield_traces(page_data)

    @dlt.resource(
        table_name="llm_observations",
        write_disposition="merge",
        primary_key="observation_id",
    )
    def observations(
        event_ts: dlt.sources.incremental[str] = dlt.sources.incremental(  # noqa: B008
            "event_ts",
            initial_value=None,
            lag=lag_seconds,
        ),
    ) -> Iterator[dict]:
        with _langfuse_client(host, public_key, secret_key) as client:
            params: dict = {"limit": page_size}
            if event_ts.start_value:
                params["fromStartTime"] = event_ts.start_value
            for page_data in _fetch_pages(client, "/api/public/observations", params):
                yield from _yield_observations(page_data)

    @dlt.resource(
        table_name="llm_scores",
        write_disposition="merge",
        primary_key="score_id",
        columns={"value": {"data_type": "double", "nullable": True}},
    )
    def scores(
        event_ts: dlt.sources.incremental[str] = dlt.sources.incremental(  # noqa: B008
            "event_ts",
            initial_value=None,
            lag=lag_seconds,
        ),
    ) -> Iterator[dict]:
        with _langfuse_client(host, public_key, secret_key) as client:
            params: dict = {"limit": page_size}
            if event_ts.start_value:
                params["fromTimestamp"] = event_ts.start_value
            for page_data in _fetch_pages(client, "/api/public/scores", params):
                yield from _yield_scores(page_data)

    return traces, observations, scores
