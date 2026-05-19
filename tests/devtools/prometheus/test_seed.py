"""Tests for the Prometheus remote write seeder."""

import pytest
from datetime import datetime, timezone

from devtools.prometheus.seed import (
    encode_varint,
    encode_label,
    encode_sample,
    encode_timeseries,
    encode_write_request,
    generate_counter_series,
    generate_histogram_series,
)


class TestProtobufEncoding:
    def test_encode_varint_small(self):
        assert encode_varint(1) == bytes([0x01])
        assert encode_varint(127) == bytes([0x7F])

    def test_encode_varint_multibyte(self):
        assert encode_varint(128) == bytes([0x80, 0x01])

    def test_encode_label_produces_bytes(self):
        data = encode_label("job", "forge")
        assert isinstance(data, bytes)
        assert len(data) > 0

    def test_encode_sample_produces_bytes(self):
        ts_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        data = encode_sample(42.0, ts_ms)
        assert isinstance(data, bytes)
        assert len(data) > 0

    def test_encode_timeseries_produces_bytes(self):
        ts_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        data = encode_timeseries(
            labels={"__name__": "test_metric", "job": "forge"},
            samples=[(1.0, ts_ms), (2.0, ts_ms + 15000)],
        )
        assert isinstance(data, bytes)
        assert len(data) > 0

    def test_encode_write_request_produces_bytes(self):
        ts_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        ts_list = [
            ({"__name__": "test_metric", "job": "forge"}, [(1.0, ts_ms)]),
        ]
        data = encode_write_request(ts_list)
        assert isinstance(data, bytes)
        assert len(data) > 0


class TestSeriesGeneration:
    def test_generate_counter_series_has_correct_label(self):
        name, label_set, samples = generate_counter_series(
            metric_name="forge_workflows_started_total",
            labels={"ticket_type": "feature"},
            final_value=50,
            n_steps=10,
            start_ms=1000000,
            step_ms=1000,
        )
        assert name == "forge_workflows_started_total"
        assert label_set["ticket_type"] == "feature"

    def test_generate_counter_series_monotonically_increases(self):
        _, _, samples = generate_counter_series(
            metric_name="forge_workflows_started_total",
            labels={"ticket_type": "bug"},
            final_value=100,
            n_steps=10,
            start_ms=1000000,
            step_ms=1000,
        )
        values = [v for v, _ in samples]
        assert values == sorted(values)
        assert values[-1] == pytest.approx(100.0)

    def test_counter_series_has_n_steps_samples(self):
        _, _, samples = generate_counter_series(
            metric_name="forge_workflows_started_total",
            labels={"ticket_type": "feature"},
            final_value=50,
            n_steps=10,
            start_ms=1000000,
            step_ms=1000,
        )
        assert len(samples) == 10

    def test_counter_series_is_monotonically_increasing(self):
        _, _, samples = generate_counter_series(
            metric_name="forge_workflows_started_total",
            labels={"ticket_type": "feature"},
            final_value=100,
            n_steps=10,
            start_ms=1000000,
            step_ms=1000,
        )
        for i in range(1, len(samples)):
            assert samples[i][0] >= samples[i - 1][0], (
                f"Value at step {i} is less than previous"
            )

    def test_generate_histogram_series_returns_bucket_count_sum(self):
        series_list = generate_histogram_series(
            metric_name="forge_phase_duration_seconds",
            labels={"phase": "generate_prd"},
            mean_s=45,
            std_s=20,
            buckets=[1, 5, 10, 30, 60, 120, 300, 600],
            observations_per_step=10,
            n_steps=10,
            start_ms=1000000,
            step_ms=1000,
        )
        names = {name for name, _, _ in series_list}
        assert any("_bucket" in n for n in names)
        assert any("_count" in n for n in names)
        assert any("_sum" in n for n in names)

    def test_histogram_series_count_has_n_steps_samples(self):
        series_list = generate_histogram_series(
            metric_name="forge_phase_duration_seconds",
            labels={"phase": "generate_prd"},
            mean_s=45,
            std_s=20,
            buckets=[1, 5, 10, 30, 60, 120, 300, 600],
            observations_per_step=10,
            n_steps=10,
            start_ms=1000000,
            step_ms=1000,
        )
        count_series = [s for s in series_list if s[0].endswith("_count")]
        assert len(count_series) == 1
        _, _, samples = count_series[0]
        assert len(samples) == 10

    def test_generate_counter_series_has_project_id_label(self):
        name, label_set, samples = generate_counter_series(
            metric_name="forge_workflows_started_total",
            labels={"ticket_type": "feature", "project_id": "OSASINFRA"},
            final_value=25,
            n_steps=10,
            start_ms=1000000,
            step_ms=1000,
        )
        assert label_set["project_id"] == "OSASINFRA"

    def test_generate_histogram_series_labels_include_project_id(self):
        series_list = generate_histogram_series(
            metric_name="forge_phase_duration_seconds",
            labels={"phase": "generate_prd", "project_id": "OSPA"},
            mean_s=45,
            std_s=20,
            buckets=[1, 5, 10, 30, 60, 120, 300, 600],
            observations_per_step=2,
            n_steps=10,
            start_ms=1000000,
            step_ms=1000,
        )
        for name, label_set, _ in series_list:
            assert label_set.get("project_id") == "OSPA", f"{name} missing project_id"
