"""dlt pipeline: Langfuse → bronze.llm_traces."""

import base64
from collections.abc import Iterator

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


@dlt.source(name="langfuse")
def langfuse_source(
    host: str,
    public_key: str,
    secret_key: str,
    limit: int = 200,
) -> DltResource:
    """Extract LLM traces from Langfuse."""

    @dlt.resource(
        table_name="llm_traces",
        write_disposition="merge",
        primary_key="trace_id",
    )
    def traces() -> Iterator[dict]:
        with _langfuse_client(host, public_key, secret_key) as client:
            page = 1
            fetched = 0
            while fetched < limit:
                response = client.get(
                    "/api/public/traces",
                    params={"page": page, "limit": min(50, limit - fetched)},
                )
                response.raise_for_status()
                data = response.json().get("data", [])
                if not data:
                    break

                for trace in data:
                    metadata = trace.get("metadata") or {}
                    latency = trace.get("latency")
                    tags = trace.get("tags") or []

                    yield {
                        "trace_id": trace["id"],
                        "name": trace.get("name") or "",
                        "ticket_key": metadata.get("ticket_key", ""),
                        "workflow_stage": metadata.get("workflow_stage", ""),
                        "latency_ms": int(latency * 1000) if latency else 0,
                        "total_cost": trace.get("totalCost") or 0.0,
                        "tags": ",".join(tags),
                        "session_id": trace.get("sessionId") or "",
                        "user_id": trace.get("userId") or "",
                        "timestamp": trace.get("timestamp"),
                    }

                fetched += len(data)
                if len(data) < 50:
                    break
                page += 1

    return traces
