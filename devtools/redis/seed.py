"""Seed forge alert data into Redis as native data structures.

Reads devtools/seed_output.json (written by devtools/langfuse/seed.py),
generates alert findings for outlier issues, and writes:

  forge:alerts:{issue_id}:{alert_type}   Hash — per-alert record
  forge:stats:alerts:summary            Hash — {total, critical, warning, cost_outlier, latency_outlier}
  forge:stats:alerts:by_type            Hash — {cost_outlier, latency_outlier}
  forge:stats:alerts:ts:cost_outlier    TimeSeries — one point per alert at fired_at
  forge:stats:alerts:ts:latency_outlier TimeSeries — one point per alert at fired_at

No JSON strings are written. All data is stored in Redis-native structures.

Run AFTER devtools/langfuse/seed.py.

Usage:
    uv run python -m devtools.redis.seed
"""

from __future__ import annotations

import os
import random
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import redis as redis_lib
from dotenv import load_dotenv

load_dotenv()

SEED_OUTPUT_PATH = Path(__file__).parent.parent.parent / "devtools" / "seed_output.json"

SUMMARY_KEY = "forge:stats:alerts:summary"
BY_TYPE_KEY = "forge:stats:alerts:by_type"
TS_KEY_PREFIX = "forge:stats:alerts:ts"
ALERT_KEY_PREFIX = "forge:alerts"
ALERT_TYPES = ["cost_outlier", "latency_outlier"]
ALERTS_INDEX = "forge:alerts:idx"
ALERTS_STREAM = "forge:alerts:stream"


@dataclass
class AlertFinding:
    issue_id: str
    project_id: str
    ticket_type: str
    alert_type: str
    severity: str
    threshold: float
    actual_value: float
    sigma: float
    trace_ids: list[str]
    fired_at: datetime
    description: str


# ── Sidecar ───────────────────────────────────────────────────────────────────


def load_seed_output() -> dict:
    """Load the sidecar JSON written by devtools/langfuse/seed.py."""
    import json

    if not SEED_OUTPUT_PATH.exists():
        raise FileNotFoundError(
            f"Seed output not found at {SEED_OUTPUT_PATH}. "
            "Run devtools/langfuse/seed.py first."
        )
    return json.loads(SEED_OUTPUT_PATH.read_text())


def compute_population_stats(issues: list[dict]) -> dict:
    """Compute mean/stdev of cost and latency from non-outlier issues."""
    normal = [i for i in issues if not i["is_outlier"] and "total_cost" in i]
    costs = [i["total_cost"] for i in normal]
    latencies = [i["total_latency_s"] for i in normal]
    return {
        "cost_mean": statistics.mean(costs),
        "cost_stdev": statistics.stdev(costs) if len(costs) > 1 else 1.0,
        "latency_mean": statistics.mean(latencies),
        "latency_stdev": statistics.stdev(latencies) if len(latencies) > 1 else 1.0,
    }


# ── Alert generation ──────────────────────────────────────────────────────────


def build_alerts_for_outlier(
    issue_id: str,
    project_id: str,
    ticket_type: str,
    trace_ids: list[str],
    base_time: datetime,
    total_cost: float,
    total_latency_s: float,
    pop_stats: dict,
) -> list[AlertFinding]:
    """Generate cost + latency AlertFindings using real values from Langfuse."""
    fired_at = base_time + timedelta(hours=random.uniform(1, 48))
    alerts = []

    cost_stdev = max(pop_stats["cost_stdev"], 0.01)
    cost_sigma = round((total_cost - pop_stats["cost_mean"]) / cost_stdev, 1)
    cost_threshold = round(pop_stats["cost_mean"] + 2 * cost_stdev, 2)
    alerts.append(
        AlertFinding(
            issue_id=issue_id,
            project_id=project_id,
            ticket_type=ticket_type,
            alert_type="cost_outlier",
            severity="critical" if cost_sigma > 3 else "warning",
            threshold=cost_threshold,
            actual_value=round(total_cost, 2),
            sigma=max(cost_sigma, 0.1),
            trace_ids=trace_ids,
            fired_at=fired_at,
            description=f"Total cost ${total_cost:.2f} exceeds {cost_sigma}σ threshold of ${cost_threshold:.2f}",
        )
    )

    lat_stdev = max(pop_stats["latency_stdev"], 0.01)
    lat_sigma = round((total_latency_s - pop_stats["latency_mean"]) / lat_stdev, 1)
    lat_threshold = round(pop_stats["latency_mean"] + 2 * lat_stdev, 1)
    alerts.append(
        AlertFinding(
            issue_id=issue_id,
            project_id=project_id,
            ticket_type=ticket_type,
            alert_type="latency_outlier",
            severity="critical" if lat_sigma > 3 else "warning",
            threshold=lat_threshold,
            actual_value=round(total_latency_s, 1),
            sigma=max(lat_sigma, 0.1),
            trace_ids=trace_ids,
            fired_at=fired_at,
            description=f"Latency {total_latency_s:.1f}s exceeds {lat_sigma}σ threshold of {lat_threshold:.1f}s",
        )
    )

    return alerts


# ── Pure aggregation helpers ──────────────────────────────────────────────────


def build_summary(findings: list[AlertFinding]) -> dict:
    """Compute aggregate counts from a list of AlertFindings."""
    return {
        "total": len(findings),
        "critical": sum(1 for f in findings if f.severity == "critical"),
        "warning": sum(1 for f in findings if f.severity == "warning"),
        "cost_outlier": sum(1 for f in findings if f.alert_type == "cost_outlier"),
        "latency_outlier": sum(
            1 for f in findings if f.alert_type == "latency_outlier"
        ),
    }


def build_hash_mapping(finding: AlertFinding) -> dict:
    """Build the HSET field mapping for a single AlertFinding."""
    return {
        "alert_id": f"{finding.issue_id}:{finding.alert_type}",
        "issue_id": finding.issue_id,
        "project_id": finding.project_id,
        "ticket_type": finding.ticket_type,
        "alert_type": finding.alert_type,
        "severity": finding.severity,
        "threshold": str(finding.threshold),
        "actual_value": str(finding.actual_value),
        "sigma": str(finding.sigma),
        "fired_at": finding.fired_at.isoformat().replace("+00:00", "Z"),
        "description": finding.description,
        "trace_ids": ",".join(finding.trace_ids),
    }


def parse_fired_at(fired_at_str: str) -> int:
    """Convert an ISO 8601 fired_at string to milliseconds epoch."""
    dt = datetime.fromisoformat(fired_at_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


# ── Redis I/O ─────────────────────────────────────────────────────────────────


def cleanup(r: redis_lib.Redis) -> None:
    """Delete all alert keys (old JSON strings and new Hashes/TimeSeries) and search index."""
    try:
        r.execute_command("FT.DROPINDEX", ALERTS_INDEX)
    except Exception:
        pass
    r.delete(ALERTS_STREAM)
    deleted = 0
    for pattern in [
        f"{ALERT_KEY_PREFIX}:*",
        SUMMARY_KEY,
        BY_TYPE_KEY,
    ]:
        keys = r.keys(pattern) if "*" in pattern else [pattern]
        if keys:
            r.delete(*keys)
            deleted += len(keys)
    for alert_type in ALERT_TYPES:
        r.delete(f"{TS_KEY_PREFIX}:{alert_type}")
        deleted += 1
    print(f"Deleted {deleted} existing alert keys")


def _ensure_timeseries_keys(r: redis_lib.Redis) -> None:
    for alert_type in ALERT_TYPES:
        try:
            r.ts().create(
                f"{TS_KEY_PREFIX}:{alert_type}", labels={"alert_type": alert_type}
            )
        except Exception:
            pass  # already exists


def _create_search_index(r: redis_lib.Redis) -> None:
    """Create a RediSearch index over forge:alerts:* hashes for table queries."""
    r.execute_command(
        "FT.CREATE",
        ALERTS_INDEX,
        "ON",
        "HASH",
        "PREFIX",
        "1",
        f"{ALERT_KEY_PREFIX}:",
        "SCHEMA",
        "issue_id",
        "TAG",
        "SORTABLE",
        "project_id",
        "TAG",
        "SORTABLE",
        "ticket_type",
        "TAG",
        "alert_type",
        "TAG",
        "SORTABLE",
        "severity",
        "TAG",
        "SORTABLE",
        "threshold",
        "NUMERIC",
        "SORTABLE",
        "actual_value",
        "NUMERIC",
        "SORTABLE",
        "sigma",
        "NUMERIC",
        "SORTABLE",
        "fired_at",
        "TEXT",
        "SORTABLE",
        "description",
        "TEXT",
    )


def write_alerts(r: redis_lib.Redis, findings: list[AlertFinding]) -> None:
    """Write per-alert Hashes, summary Hash, by-type Hash, and TimeSeries points."""
    summary = build_summary(findings)
    r.hset(SUMMARY_KEY, mapping={k: str(v) for k, v in summary.items()})
    r.hset(
        BY_TYPE_KEY,
        mapping={
            "cost_outlier": str(summary["cost_outlier"]),
            "latency_outlier": str(summary["latency_outlier"]),
        },
    )

    _ensure_timeseries_keys(r)
    _create_search_index(r)

    for finding in findings:
        mapping = build_hash_mapping(finding)
        r.hset(
            f"{ALERT_KEY_PREFIX}:{finding.issue_id}:{finding.alert_type}",
            mapping=mapping,
        )
        r.xadd(ALERTS_STREAM, mapping)
        fired_ms = parse_fired_at(finding.fired_at.isoformat().replace("+00:00", "Z"))
        r.ts().add(
            f"{TS_KEY_PREFIX}:{finding.alert_type}", fired_ms, 1, duplicate_policy="sum"
        )


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    seed_data = load_seed_output()
    issues = seed_data["issues"]

    r = redis_lib.Redis(
        host="localhost",
        port=int(os.getenv("REDIS_PORT", "6380")),
        decode_responses=True,
    )

    print("Cleaning up existing alert keys...")
    cleanup(r)

    print("Computing population statistics from Langfuse data...")
    pop_stats = compute_population_stats(issues)
    print(
        f"  Cost: mean=${pop_stats['cost_mean']:.2f}, stdev=${pop_stats['cost_stdev']:.2f}"
    )
    print(
        f"  Latency: mean={pop_stats['latency_mean']:.1f}s, stdev={pop_stats['latency_stdev']:.1f}s"
    )

    print(f"Generating alerts for {len(issues)} issues...")
    all_findings: list[AlertFinding] = []
    for issue in issues:
        if not issue["is_outlier"]:
            continue
        base_time_str = issue.get("base_time", "")
        base_time = (
            datetime.fromisoformat(base_time_str.replace("Z", "+00:00"))
            if base_time_str
            else datetime.now(timezone.utc)
        )
        all_findings.extend(
            build_alerts_for_outlier(
                issue_id=issue["issue_id"],
                project_id=issue.get("project_id", ""),
                ticket_type=issue["ticket_type"],
                trace_ids=issue.get("trace_ids", []),
                base_time=base_time,
                total_cost=issue["total_cost"],
                total_latency_s=issue["total_latency_s"],
                pop_stats=pop_stats,
            )
        )

    print(
        f"Writing {len(all_findings)} alerts for {len(all_findings) // 2} outlier issues..."
    )
    write_alerts(r, all_findings)

    summary = r.hgetall(SUMMARY_KEY)
    print(f"Done. Summary: {summary}")


if __name__ == "__main__":
    main()
