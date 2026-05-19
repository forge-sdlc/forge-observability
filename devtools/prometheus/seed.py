"""Write backdated Forge Prometheus metrics via remote write API.

Pushes a snapshot of Forge metric values to Prometheus via the remote write
API (protobuf + snappy). Counters are set to their final seed values;
histograms are built from a realistic sample of observations. All samples
use the current timestamp so no special Prometheus flags are needed.

Deletes existing forge_* series before writing (requires --web.enable-admin-api).

Run AFTER devtools/langfuse/seed.py.

Usage:
    uv run python -m devtools.prometheus.seed

Prerequisites:
    brew install snappy   # macOS
    uv add python-snappy

    Forge Prometheus (forge/docker-compose.yml) must be started with two extra flags.
    Add these to the prometheus service command list in forge/docker-compose.yml:

        - '--web.enable-remote-write-receiver'          # enables /api/v1/write
        - '--web.enable-admin-api'                      # enables delete_series for cleanup

    Out-of-order ingestion must be enabled in prometheus.yml (in the forge repo):

        storage:
          tsdb:
            out_of_order_time_window: 15d

    Then restart: podman compose -f forge/docker-compose.yml up -d prometheus
    (use up -d, not restart -- restart does not reload compose configuration)

    Without --web.enable-remote-write-receiver the /api/v1/write endpoint returns 404.
    Without --web.enable-admin-api the cleanup step is skipped (data accumulates on re-runs).
    Without out_of_order_time_window in prometheus.yml, Prometheus rejects backdated samples
    with HTTP 400 out of bounds -- only samples within ~1 hour of now are accepted by default.

    Prometheus runs on host port 9092 in Forge (mapped from container port 9090).
    Set PROMETHEUS_PORT=9092 in .env (already the default).
"""

from __future__ import annotations

import json
import os
import random
import struct
from datetime import datetime, timezone
from pathlib import Path

import requests
import snappy
from dotenv import load_dotenv

load_dotenv()

SEED_DAYS = 1
STEP_SECONDS = 15  # 15-second scrape interval — matches production
SEED_OUTPUT_PATH = Path(__file__).parent.parent.parent / "devtools" / "seed_output.json"

WORKFLOW_PHASES = [
    "route_entry",
    "generate_prd",
    "regenerate_prd",
    "generate_spec",
    "decompose_epics",
    "generate_tasks",
    "implement_task",
    "local_review",
    "create_pr",
    "ci_evaluator",
    "attempt_ci_fix",
    "human_review_gate",
    "analyze_bug",
    "implement_bug_fix",
]

TASK_TYPES = [
    "implement_task",
    "implement_bug_fix",
    "local_review",
    "generate_prd",
    "generate_spec",
]

APPROVAL_STAGES = ["generate_prd", "generate_spec", "human_review_gate"]

PHASE_DURATION_PARAMS = {
    "route_entry": (2, 1),
    "generate_prd": (45, 20),
    "regenerate_prd": (35, 15),
    "generate_spec": (30, 12),
    "decompose_epics": (20, 8),
    "generate_tasks": (15, 6),
    "implement_task": (60, 30),
    "local_review": (25, 10),
    "create_pr": (5, 2),
    "ci_evaluator": (8, 3),
    "attempt_ci_fix": (40, 20),
    "human_review_gate": (20, 8),
    "analyze_bug": (35, 15),
    "implement_bug_fix": (50, 25),
}

HISTOGRAM_BUCKETS = [1, 5, 10, 30, 60, 120, 300, 600]
API_LATENCY_BUCKETS = [0.1, 0.5, 1, 2, 5, 10, 30]


# ── Minimal protobuf encoder ──────────────────────────────────────────────────


def encode_varint(n: int) -> bytes:
    buf = []
    while True:
        b = n & 0x7F
        n >>= 7
        buf.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(buf)


def _field(number: int, wire: int, data: bytes) -> bytes:
    return encode_varint((number << 3) | wire) + data


def _len_field(number: int, payload: bytes) -> bytes:
    return _field(number, 2, encode_varint(len(payload)) + payload)


def encode_label(name: str, value: str) -> bytes:
    return _len_field(1, name.encode()) + _len_field(2, value.encode())


def encode_sample(value: float, timestamp_ms: int) -> bytes:
    # field 1: double (wire type 1), field 2: int64 varint (wire type 0)
    return (
        bytes([0x09])
        + struct.pack("<d", value)
        + bytes([0x10])
        + encode_varint(timestamp_ms)
    )


def encode_timeseries(
    labels: dict[str, str],
    samples: list[tuple[float, int]],
) -> bytes:
    label_bytes = b"".join(
        _len_field(1, encode_label(k, v)) for k, v in sorted(labels.items())
    )
    sample_bytes = b"".join(_len_field(2, encode_sample(v, ts)) for v, ts in samples)
    return label_bytes + sample_bytes


def encode_write_request(
    time_series: list[tuple[dict[str, str], list[tuple[float, int]]]],
) -> bytes:
    return b"".join(
        _len_field(1, encode_timeseries(labels, samples))
        for labels, samples in time_series
    )


# ── Time-series generators ────────────────────────────────────────────────────


def _now_ms() -> int:
    """Current time in milliseconds."""
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def generate_counter_series(
    metric_name: str,
    labels: dict[str, str],
    final_value: float,
    n_steps: int,
    start_ms: int,
    step_ms: int,
) -> tuple[str, dict, list[tuple[float, int]]]:
    """Counter growing linearly from 0 to final_value over n_steps."""
    samples = []
    for i in range(n_steps):
        t = start_ms + i * step_ms
        value = final_value * (i + 1) / n_steps
        samples.append((value, t))
    return metric_name, labels, samples


def generate_histogram_series(
    metric_name: str,
    labels: dict[str, str],
    mean_s: float,
    std_s: float,
    buckets: list[float],
    observations_per_step: int,
    n_steps: int,
    start_ms: int,
    step_ms: int,
) -> list[tuple[str, dict, list[tuple[float, int]]]]:
    """Histogram growing over n_steps, adding observations_per_step at each step."""
    bucket_counts = [0] * (len(buckets) + 1)  # +1 for +Inf
    total_count = 0
    total_sum = 0.0

    bucket_samples = [[] for _ in range(len(buckets) + 1)]
    count_samples = []
    sum_samples = []

    for i in range(n_steps):
        t = start_ms + i * step_ms
        for _ in range(observations_per_step):
            obs = max(0.1, random.gauss(mean_s, std_s))
            total_sum += obs
            total_count += 1
            for j, boundary in enumerate(buckets):
                if obs <= boundary:
                    bucket_counts[j] += 1
            bucket_counts[-1] += 1  # +Inf

        for j in range(len(buckets) + 1):
            bucket_samples[j].append((float(bucket_counts[j]), t))
        count_samples.append((float(total_count), t))
        sum_samples.append((total_sum, t))

    all_series = []
    for j, boundary in enumerate(buckets):
        all_series.append(
            (
                f"{metric_name}_bucket",
                {**labels, "le": str(boundary)},
                bucket_samples[j],
            )
        )
    all_series.append(
        (f"{metric_name}_bucket", {**labels, "le": "+Inf"}, bucket_samples[-1])
    )
    all_series.append((f"{metric_name}_count", labels, count_samples))
    all_series.append((f"{metric_name}_sum", labels, sum_samples))
    return all_series


# ── Prometheus I/O ────────────────────────────────────────────────────────────


def _push(time_series_list: list[tuple[str, dict, list]], prometheus_url: str) -> None:
    payload = [
        ({"__name__": name, **labels}, samples)
        for name, labels, samples in time_series_list
    ]
    encoded = encode_write_request(payload)
    compressed = snappy.compress(encoded)
    resp = requests.post(
        f"{prometheus_url}/api/v1/write",
        data=compressed,
        headers={
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "snappy",
            "X-Prometheus-Remote-Write-Version": "0.1.0",
        },
        timeout=60,
    )
    resp.raise_for_status()


def _delete_existing_series(url: str) -> None:
    """Delete all forge_* series via the Prometheus admin API."""
    resp = requests.post(
        f"{url}/api/v1/admin/tsdb/delete_series",
        params={"match[]": '{__name__=~"forge_.+"}'},
        timeout=30,
    )
    if resp.status_code not in (204, 200):
        print(f"Warning: delete_series returned {resp.status_code}: {resp.text}")
    requests.post(f"{url}/api/v1/admin/tsdb/clean_tombstones", timeout=30)
    print("Deleted existing forge_* series from Prometheus")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    seed_data = json.loads(SEED_OUTPUT_PATH.read_text())
    projects = seed_data.get("projects", ["OSASINFRA", "OSPA"])
    n_features_per_project = seed_data.get("n_features_per_project", 25)
    n_bugs_per_project = seed_data.get("n_bugs_per_project", 50)

    n_steps = (SEED_DAYS * 86400) // STEP_SECONDS + 1  # 85 — last sample lands at now
    now_ms = _now_ms()
    step_ms = STEP_SECONDS * 1000
    start_ms = now_ms - (n_steps - 1) * step_ms

    port = os.getenv("PROMETHEUS_PORT", "9092")
    url = f"http://localhost:{port}"

    print(
        f"Pushing Prometheus metric snapshot ({projects}, features={n_features_per_project}/project, bugs={n_bugs_per_project}/project)..."
    )
    _delete_existing_series(url)

    all_series: list[tuple[str, dict, list]] = []

    for project_id in projects:
        for ticket_type, total in [
            ("feature", n_features_per_project),
            ("bug", n_bugs_per_project),
        ]:
            lbl = {"ticket_type": ticket_type, "project_id": project_id}
            all_series.append(
                generate_counter_series(
                    "forge_workflows_started_total",
                    lbl,
                    total,
                    n_steps,
                    start_ms,
                    step_ms,
                )
            )
            all_series.append(
                generate_counter_series(
                    "forge_workflows_completed_total",
                    {
                        **lbl,
                        "final_node": "aggregate_feature_status"
                        if ticket_type == "feature"
                        else "human_review_gate",
                    },
                    int(total * 0.94),
                    n_steps,
                    start_ms,
                    step_ms,
                )
            )
            all_series.append(
                generate_counter_series(
                    "forge_workflows_failed_total",
                    {**lbl, "error_type": "ci_failure"},
                    int(total * 0.06),
                    n_steps,
                    start_ms,
                    step_ms,
                )
            )

    for project_id in projects:
        for phase, (mean_s, std_s) in PHASE_DURATION_PARAMS.items():
            all_series.extend(
                generate_histogram_series(
                    "forge_phase_duration_seconds",
                    {"phase": phase, "project_id": project_id},
                    mean_s=mean_s,
                    std_s=std_s,
                    buckets=HISTOGRAM_BUCKETS,
                    observations_per_step=2,
                    n_steps=n_steps,
                    start_ms=start_ms,
                    step_ms=step_ms,
                )
            )

    for project_id in projects:
        for task_type in TASK_TYPES:
            all_series.extend(
                generate_histogram_series(
                    "forge_agent_duration_seconds",
                    {"task_type": task_type, "project_id": project_id},
                    mean_s=40,
                    std_s=20,
                    buckets=[5, 10, 30, 60, 120, 300, 600],
                    observations_per_step=3,
                    n_steps=n_steps,
                    start_ms=start_ms,
                    step_ms=step_ms,
                )
            )
            all_series.append(
                generate_counter_series(
                    "forge_agent_invocations_total",
                    {"task_type": task_type, "project_id": project_id},
                    final_value=random.randint(80, 200),
                    n_steps=n_steps,
                    start_ms=start_ms,
                    step_ms=step_ms,
                )
            )

    for project_id in projects:
        for stage in APPROVAL_STAGES:
            all_series.append(
                generate_counter_series(
                    "forge_approvals_total",
                    {"stage": stage, "project_id": project_id},
                    final_value=random.randint(80, 140),
                    n_steps=n_steps,
                    start_ms=start_ms,
                    step_ms=step_ms,
                )
            )
            all_series.append(
                generate_counter_series(
                    "forge_revisions_requested_total",
                    {"stage": stage, "project_id": project_id},
                    final_value=random.randint(10, 40),
                    n_steps=n_steps,
                    start_ms=start_ms,
                    step_ms=step_ms,
                )
            )

    for project_id in projects:
        all_series.append(
            generate_counter_series(
                "forge_ci_fix_attempts_total",
                {"repo": "main-app", "result": "success", "project_id": project_id},
                final_value=random.randint(30, 60),
                n_steps=n_steps,
                start_ms=start_ms,
                step_ms=step_ms,
            )
        )
        all_series.append(
            generate_counter_series(
                "forge_ci_fix_attempts_total",
                {"repo": "main-app", "result": "failure", "project_id": project_id},
                final_value=random.randint(5, 15),
                n_steps=n_steps,
                start_ms=start_ms,
                step_ms=step_ms,
            )
        )

    for project_id in projects:
        for service, ops in [
            ("jira", ["get_issue", "update_issue"]),
            ("github", ["create_pr", "get_check_runs"]),
            ("langfuse", ["create_trace"]),
        ]:
            for op in ops:
                all_series.extend(
                    generate_histogram_series(
                        "forge_external_api_latency_seconds",
                        {"service": service, "operation": op, "project_id": project_id},
                        mean_s=1.5,
                        std_s=0.8,
                        buckets=API_LATENCY_BUCKETS,
                        observations_per_step=5,
                        n_steps=n_steps,
                        start_ms=start_ms,
                        step_ms=step_ms,
                    )
                )

    batch_size = 20
    total_batches = -(-len(all_series) // batch_size)
    for i in range(0, len(all_series), batch_size):
        batch = all_series[i : i + batch_size]
        print(
            f"  Pushing batch {i // batch_size + 1}/{total_batches} ({len(batch)} series)..."
        )
        _push(batch, url)

    print(f"Done. Pushed {len(all_series)} time-series to Prometheus.")


if __name__ == "__main__":
    main()
