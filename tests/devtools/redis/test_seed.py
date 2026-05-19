"""Tests for the Redis alert seeder."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from devtools.redis.seed import (
    ALERT_KEY_PREFIX,
    ALERT_TYPES,
    BY_TYPE_KEY,
    SUMMARY_KEY,
    TS_KEY_PREFIX,
    AlertFinding,
    build_alerts_for_outlier,
    build_hash_mapping,
    build_summary,
    compute_population_stats,
    load_seed_output,
    parse_fired_at,
)


def _make_finding(alert_type="cost_outlier", severity="critical") -> AlertFinding:
    return AlertFinding(
        issue_id="OSASINFRA-1",
        project_id="OSASINFRA",
        ticket_type="feature",
        alert_type=alert_type,
        severity=severity,
        threshold=3.5,
        actual_value=9.2,
        sigma=3.1,
        trace_ids=["abc", "def"],
        fired_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc),
        description="Cost spike",
    )


# ── build_alerts_for_outlier ──────────────────────────────────────────────────

_BASE_TIME = datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
_POP_STATS = {
    "cost_mean": 10.0,
    "cost_stdev": 3.0,
    "latency_mean": 200.0,
    "latency_stdev": 50.0,
}


def test_outlier_gets_cost_and_latency_alerts():
    findings = build_alerts_for_outlier(
        "OSASINFRA-1",
        "OSASINFRA",
        "feature",
        ["t1", "t2"],
        _BASE_TIME,
        total_cost=25.0,
        total_latency_s=500.0,
        pop_stats=_POP_STATS,
    )
    assert {f.alert_type for f in findings} == {"cost_outlier", "latency_outlier"}


def test_outlier_cost_alert_severity_and_value():
    findings = build_alerts_for_outlier(
        "OSASINFRA-1",
        "OSASINFRA",
        "feature",
        ["t1"],
        _BASE_TIME,
        total_cost=25.0,
        total_latency_s=500.0,
        pop_stats=_POP_STATS,
    )
    cost = next(f for f in findings if f.alert_type == "cost_outlier")
    assert cost.severity in ("critical", "warning")
    assert cost.actual_value > cost.threshold


def test_outlier_latency_alert_carries_trace_ids():
    findings = build_alerts_for_outlier(
        "OSPA-26",
        "OSPA",
        "bug",
        ["t1", "t2"],
        _BASE_TIME,
        total_cost=25.0,
        total_latency_s=500.0,
        pop_stats=_POP_STATS,
    )
    lat = next(f for f in findings if f.alert_type == "latency_outlier")
    assert lat.trace_ids == ["t1", "t2"]


def test_outlier_fired_at_derived_from_base_time():
    findings = build_alerts_for_outlier(
        "OSASINFRA-1",
        "OSASINFRA",
        "feature",
        ["t1"],
        _BASE_TIME,
        total_cost=25.0,
        total_latency_s=500.0,
        pop_stats=_POP_STATS,
    )
    for f in findings:
        offset = (f.fired_at - _BASE_TIME).total_seconds() / 3600
        assert 1 <= offset <= 48


def test_outlier_project_id_set_on_findings():
    findings = build_alerts_for_outlier(
        "OSPA-1",
        "OSPA",
        "bug",
        [],
        _BASE_TIME,
        total_cost=25.0,
        total_latency_s=500.0,
        pop_stats=_POP_STATS,
    )
    for f in findings:
        assert f.project_id == "OSPA"


# ── build_summary ─────────────────────────────────────────────────────────────


def _make_findings() -> list[AlertFinding]:
    return [
        _make_finding("cost_outlier", "critical"),
        _make_finding("cost_outlier", "warning"),
        _make_finding("latency_outlier", "critical"),
        _make_finding("latency_outlier", "warning"),
        _make_finding("cost_outlier", "critical"),
    ]


def test_build_summary_total():
    assert build_summary(_make_findings())["total"] == 5


def test_build_summary_critical():
    assert build_summary(_make_findings())["critical"] == 3


def test_build_summary_warning():
    assert build_summary(_make_findings())["warning"] == 2


def test_build_summary_cost_outlier():
    assert build_summary(_make_findings())["cost_outlier"] == 3


def test_build_summary_latency_outlier():
    assert build_summary(_make_findings())["latency_outlier"] == 2


def test_build_summary_empty():
    assert build_summary([]) == {
        "total": 0,
        "critical": 0,
        "warning": 0,
        "cost_outlier": 0,
        "latency_outlier": 0,
    }


# ── build_hash_mapping ────────────────────────────────────────────────────────


def test_build_hash_mapping_all_fields():
    mapping = build_hash_mapping(_make_finding())
    assert mapping["issue_id"] == "OSASINFRA-1"
    assert mapping["project_id"] == "OSASINFRA"
    assert mapping["ticket_type"] == "feature"
    assert mapping["alert_type"] == "cost_outlier"
    assert mapping["severity"] == "critical"
    assert mapping["threshold"] == "3.5"
    assert mapping["actual_value"] == "9.2"
    assert mapping["sigma"] == "3.1"
    assert mapping["fired_at"] == "2026-05-10T12:00:00Z"
    assert mapping["trace_ids"] == "abc,def"


def test_build_hash_mapping_empty_trace_ids():
    f = _make_finding()
    f.trace_ids = []
    assert build_hash_mapping(f)["trace_ids"] == ""


# ── parse_fired_at ────────────────────────────────────────────────────────────


def test_parse_fired_at_z_suffix():
    ms = parse_fired_at("2026-05-10T12:00:00Z")
    assert ms == int(
        datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
    )


def test_parse_fired_at_offset_suffix():
    ms = parse_fired_at("2026-05-10T12:00:00+00:00")
    assert ms == int(
        datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
    )


# ── Key constants ─────────────────────────────────────────────────────────────


def test_key_constants():
    assert SUMMARY_KEY == "forge:stats:alerts:summary"
    assert BY_TYPE_KEY == "forge:stats:alerts:by_type"
    assert TS_KEY_PREFIX == "forge:stats:alerts:ts"
    assert ALERT_KEY_PREFIX == "forge:alerts"
    assert set(ALERT_TYPES) == {"cost_outlier", "latency_outlier"}


# ── load_seed_output ──────────────────────────────────────────────────────────


def test_load_seed_output_structure(tmp_path, monkeypatch):
    import devtools.redis.seed as seed_mod

    output = {
        "seeded_at": "2026-05-15T00:00:00Z",
        "projects": ["OSASINFRA", "OSPA"],
        "n_features": 50,
        "n_bugs": 100,
        "n_features_per_project": 25,
        "n_bugs_per_project": 50,
        "window_days": 730,
        "issues": [
            {
                "issue_id": "OSASINFRA-1",
                "project_id": "OSASINFRA",
                "ticket_type": "feature",
                "is_outlier": True,
                "base_time": "2024-01-01T00:00:00Z",
                "trace_ids": ["t1"],
            },
        ],
    }
    p = tmp_path / "seed_output.json"
    p.write_text(json.dumps(output))
    monkeypatch.setattr(seed_mod, "SEED_OUTPUT_PATH", p)
    data = load_seed_output()
    assert data["n_features"] == 50
    assert data["issues"][0]["project_id"] == "OSASINFRA"


# ── compute_population_stats ──────────────────────────────────────────────────


def test_compute_population_stats_basic():
    issues = [
        {"is_outlier": False, "total_cost": 10.0, "total_latency_s": 200.0},
        {"is_outlier": False, "total_cost": 12.0, "total_latency_s": 220.0},
        {"is_outlier": False, "total_cost": 8.0, "total_latency_s": 180.0},
        {"is_outlier": True, "total_cost": 50.0, "total_latency_s": 800.0},
    ]
    stats = compute_population_stats(issues)
    assert stats["cost_mean"] == pytest.approx(10.0, rel=0.01)
    assert stats["cost_stdev"] > 0
    assert stats["latency_mean"] == pytest.approx(200.0, rel=0.01)
    assert stats["latency_stdev"] > 0


def test_compute_population_stats_excludes_outliers():
    issues = [
        {"is_outlier": False, "total_cost": 5.0, "total_latency_s": 100.0},
        {"is_outlier": False, "total_cost": 5.0, "total_latency_s": 100.0},
        {"is_outlier": True, "total_cost": 500.0, "total_latency_s": 9999.0},
    ]
    stats = compute_population_stats(issues)
    assert stats["cost_mean"] == pytest.approx(5.0)
    assert stats["latency_mean"] == pytest.approx(100.0)


def test_alert_uses_real_cost_value():
    findings = build_alerts_for_outlier(
        "OSASINFRA-1",
        "OSASINFRA",
        "feature",
        ["t1"],
        _BASE_TIME,
        total_cost=42.50,
        total_latency_s=350.0,
        pop_stats=_POP_STATS,
    )
    cost_alert = next(f for f in findings if f.alert_type == "cost_outlier")
    assert cost_alert.actual_value == 42.50
    latency_alert = next(f for f in findings if f.alert_type == "latency_outlier")
    assert latency_alert.actual_value == 350.0
