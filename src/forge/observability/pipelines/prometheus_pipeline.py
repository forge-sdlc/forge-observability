"""dlt pipeline: Prometheus → bronze.app_metrics."""

import contextlib
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import dlt
import httpx
from dlt.sources import DltResource

logger = logging.getLogger(__name__)


@dlt.source(name="prometheus")
def prometheus_source(
    prometheus_url: str,
    lookback_minutes: int = 10,
) -> DltResource:
    """Extract all Prometheus metrics as time-series rows."""

    @dlt.resource(
        table_name="app_metrics",
        write_disposition="append",
    )
    def app_metrics() -> Iterator[dict]:
        now = datetime.now(tz=UTC)
        start = now - timedelta(minutes=lookback_minutes)

        with httpx.Client(base_url=prometheus_url, timeout=15.0) as client:
            try:
                resp = client.get("/api/v1/label/__name__/values")
                resp.raise_for_status()
                metric_names: list[str] = [
                    m for m in resp.json().get("data", []) if m.startswith("forge")
                ]
            except httpx.HTTPError as exc:
                logger.warning(f"Failed to discover Prometheus metrics: {exc}")
                return

            for metric_name in metric_names:
                try:
                    resp = client.get(
                        "/api/v1/query_range",
                        params={
                            "query": metric_name,
                            "start": start.isoformat(),
                            "end": now.isoformat(),
                            "step": "60s",
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    for result in data.get("data", {}).get("result", []):
                        labels = result.get("metric", {})
                        for ts, value in result.get("values", []):
                            with contextlib.suppress(ValueError, TypeError):
                                yield {
                                    "metric_name": metric_name,
                                    "value": float(value),
                                    "labels": str(labels),
                                    "sampled_at": datetime.fromtimestamp(
                                        float(ts), tz=UTC
                                    ),
                                }

                except httpx.HTTPError as exc:
                    logger.warning(
                        f"Prometheus query failed for {metric_name!r}: {exc}"
                    )

    return app_metrics
