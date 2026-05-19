import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[3] / "devtools" / "langfuse"))

from seed import (
    _seed_trace,
    MODEL_DEEP_RESEARCH,
    seed_feature_ticket,
    seed_bug_ticket,
    _compute_cost,
)  # noqa: E402


# ── Config ────────────────────────────────────────────────────────────────────


def test_load_config_raises_when_public_key_missing(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    from seed import load_config

    with pytest.raises(ValueError, match="LANGFUSE_PUBLIC_KEY"):
        load_config()


def test_load_config_raises_when_secret_key_missing(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    from seed import load_config

    with pytest.raises(ValueError, match="LANGFUSE_SECRET_KEY"):
        load_config()


def test_load_config_raises_when_both_keys_missing(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    from seed import load_config

    with pytest.raises(ValueError):
        load_config()


def test_load_config_reads_from_env(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_HOST", "localhost")
    monkeypatch.setenv("LANGFUSE_PORT", "3000")
    from seed import load_config

    cfg = load_config()
    assert cfg.public_key == "pk-test"
    assert cfg.secret_key == "sk-test"


# ── Pricing constants ────────────────────────────────────────────────────────


def test_model_pricing_has_correct_prices():
    from seed import MODEL_PRICING

    assert MODEL_PRICING["claude-opus-4-7"]["input_price"] == 0.000015
    assert MODEL_PRICING["claude-opus-4-7"]["output_price"] == 0.000075
    assert MODEL_PRICING["claude-opus-4-5@20251101"]["input_price"] == 0.000015
    assert MODEL_PRICING["claude-sonnet-4-6"]["input_price"] == 0.000003
    assert MODEL_PRICING["gemini-2.5-pro"]["input_price"] == 0.00000125
    assert MODEL_PRICING["gemini-2.5-pro"]["output_price"] == 0.00001
    assert MODEL_PRICING["claude-haiku-4-5@20251001"]["input_price"] == 0.000001


# ── Distribution helpers ──────────────────────────────────────────────────────


def test_sample_tokens_normal_range():
    from seed import sample_tokens

    for _ in range(200):
        inp, out = sample_tokens("routing", is_outlier=False)
        assert 10_000 <= inp
        assert 100 <= out


def test_sample_tokens_outlier_range_is_larger():
    from seed import sample_tokens

    normal_inputs = [sample_tokens("deep_research", False)[0] for _ in range(100)]
    outlier_inputs = [sample_tokens("deep_research", True)[0] for _ in range(100)]
    assert sum(outlier_inputs) / len(outlier_inputs) > sum(normal_inputs) / len(
        normal_inputs
    )


def test_sample_duration_returns_timedelta():
    from seed import sample_duration
    import datetime

    d = sample_duration("routing", is_outlier=False)
    assert isinstance(d, datetime.timedelta)
    assert d.total_seconds() > 0


def test_sample_duration_outlier_longer_on_average():
    from seed import sample_duration

    normal = [
        sample_duration("code_writing", False).total_seconds() for _ in range(100)
    ]
    outlier = [
        sample_duration("code_writing", True).total_seconds() for _ in range(100)
    ]
    assert sum(outlier) / len(outlier) > sum(normal) / len(normal)


def test_extra_cycles_returns_non_negative_int():
    from seed import extra_cycles

    for step in ["generate_spec", "implement_task", "local_review"]:
        result = extra_cycles(step, is_outlier=False)
        assert isinstance(result, int) and result >= 0


def test_extra_cycles_outlier_produces_more_cycles_on_average():
    from seed import extra_cycles

    normal = [extra_cycles("implement_task", False) for _ in range(500)]
    outlier = [extra_cycles("implement_task", True) for _ in range(500)]
    assert sum(outlier) / len(outlier) > sum(normal) / len(normal)


def test_random_past_datetime_is_in_past():
    from seed import random_past_datetime

    for _ in range(50):
        dt = random_past_datetime(days=730)
        now = datetime.now(tz=timezone.utc)
        assert dt < now
        assert dt > now - timedelta(days=731)


# ── Content generators ────────────────────────────────────────────────────────


def test_make_ticket_key():
    from seed import make_ticket_key

    assert make_ticket_key("AISOS", 42) == "AISOS-42"


def test_project_constants():
    from seed import PROJECTS, _N_FEATURES_PER_PROJECT, _N_BUGS_PER_PROJECT

    assert set(PROJECTS) == {"OSASINFRA", "OSPA"}
    assert _N_FEATURES_PER_PROJECT == 25
    assert _N_BUGS_PER_PROJECT == 50


def test_make_ticket_key_with_osasinfra():
    from seed import make_ticket_key

    assert make_ticket_key("OSASINFRA", 1) == "OSASINFRA-1"
    assert make_ticket_key("OSPA", 75) == "OSPA-75"


def test_seed_feature_ticket_accepts_project_id():
    client, _, _ = _make_mock_client()
    t = datetime(2024, 6, 1, tzinfo=timezone.utc)
    trace_ids, cost, latency = seed_feature_ticket(
        client, "OSASINFRA-1", "Test feature", t, project_id="OSASINFRA"
    )
    assert isinstance(trace_ids, list)
    assert len(trace_ids) > 0


def test_seed_bug_ticket_accepts_project_id():
    client, _, _ = _make_mock_client()
    t = datetime(2024, 6, 1, tzinfo=timezone.utc)
    trace_ids, cost, latency = seed_bug_ticket(
        client, "OSPA-26", "Test bug", t, project_id="OSPA"
    )
    assert isinstance(trace_ids, list)
    assert len(trace_ids) > 0


def test_make_prd_prompt_contains_summary():
    from seed import make_prd_prompt

    prompt = make_prd_prompt("AISOS-1", "Add login feature")
    assert "Add login feature" in prompt
    assert "Product Requirements Document" in prompt


def test_make_spec_prompt_contains_prd():
    from seed import make_spec_prompt

    prompt = make_spec_prompt("AISOS-1", "Add login feature", "prd content here")
    assert "prd content here" in prompt
    assert "specification" in prompt.lower()


def test_make_rca_prompt_contains_summary():
    from seed import make_rca_prompt

    prompt = make_rca_prompt("AISOS-1", "Login broken on Safari")
    assert "Login broken on Safari" in prompt
    assert "root cause" in prompt.lower()


def test_make_prd_content_has_required_sections():
    from seed import make_prd_content

    content = make_prd_content("Add user authentication")
    assert "# Product Requirements Document" in content
    assert "Overview" in content
    assert "Goals" in content
    assert "User Stories" in content


def test_make_rca_content_has_required_sections():
    from seed import make_rca_content

    content = make_rca_content("Login button not working")
    assert "# Root Cause Analysis" in content
    assert "Root Cause" in content
    assert "Proposed Fix" in content


# ── Builder helpers ───────────────────────────────────────────────────────────


def _make_mock_trace():
    trace = MagicMock()
    root_span = MagicMock()
    model_span = MagicMock()
    trace.span.return_value = root_span
    root_span.span.return_value = model_span
    root_span.generation.return_value = MagicMock()
    model_span.generation.return_value = MagicMock()
    return trace, root_span, model_span


def test_build_langgraph_root_creates_root_span_on_trace():
    from seed import build_langgraph_root

    trace, root_span, _ = _make_mock_trace()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result_root, result_t = build_langgraph_root(trace, t, "test prompt")
    trace.span.assert_called_once()
    call_kwargs = trace.span.call_args.kwargs
    assert call_kwargs["name"] == "LangGraph"
    assert result_root is root_span
    assert result_t > t


def test_build_langgraph_root_creates_middleware_spans():
    from seed import build_langgraph_root

    trace, root_span, _ = _make_mock_trace()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    build_langgraph_root(trace, t, "test prompt")
    span_names = [c.kwargs["name"] for c in root_span.span.call_args_list]
    assert "SkillsMiddleware.before_agent" in span_names
    assert "PatchToolCallsMiddleware.before_agent" in span_names


def test_add_model_cycle_creates_model_chain_and_generation():
    from seed import build_langgraph_root, add_model_cycle

    trace, root_span, model_span = _make_mock_trace()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    root, t = build_langgraph_root(trace, t, "prompt")
    t2 = add_model_cycle(
        root,
        t,
        model="claude-opus-4-7",
        prompt_tokens=10_000,
        completion_tokens=500,
        tool_calls=[("read_file", {"path": "/foo"}, {"content": "bar"})],
    )
    model_chain_names = [
        c.kwargs["name"]
        for c in root_span.span.call_args_list
        if c.kwargs.get("name") == "model"
    ]
    assert len(model_chain_names) >= 1
    assert t2 > t


def test_add_model_cycle_uses_chatanthropicvertex_for_claude():
    from seed import build_langgraph_root, add_model_cycle

    trace, root_span, model_span = _make_mock_trace()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    root, t = build_langgraph_root(trace, t, "prompt")
    add_model_cycle(
        root,
        t,
        model="claude-opus-4-7",
        prompt_tokens=1000,
        completion_tokens=100,
        tool_calls=[],
    )
    gen_calls = model_span.generation.call_args_list
    assert any(c.kwargs.get("name") == "ChatAnthropicVertex" for c in gen_calls)


def test_add_model_cycle_uses_chatvertexai_for_gemini():
    from seed import build_langgraph_root, add_model_cycle

    trace, root_span, model_span = _make_mock_trace()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    root, t = build_langgraph_root(trace, t, "prompt")
    add_model_cycle(
        root,
        t,
        model="gemini-2.5-pro",
        prompt_tokens=1000,
        completion_tokens=100,
        tool_calls=[],
    )
    gen_calls = model_span.generation.call_args_list
    assert any(c.kwargs.get("name") == "ChatVertexAI" for c in gen_calls)


def test_add_model_cycle_creates_tool_chains_for_each_tool():
    from seed import build_langgraph_root, add_model_cycle

    trace, root_span, model_span = _make_mock_trace()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    root, t = build_langgraph_root(trace, t, "prompt")
    tools = [("read_file", {}, {}), ("search_code", {}, {})]
    add_model_cycle(
        root,
        t,
        model="claude-opus-4-7",
        prompt_tokens=1000,
        completion_tokens=100,
        tool_calls=tools,
    )
    tools_chain_calls = [
        c for c in root_span.span.call_args_list if c.kwargs.get("name") == "tools"
    ]
    assert len(tools_chain_calls) == 2


# ── Feature planning step seeders ────────────────────────────────────────────


def _make_mock_client():
    client = MagicMock()
    trace = MagicMock()
    root_span = MagicMock()
    model_span = MagicMock()
    client.trace.return_value = trace
    trace.span.return_value = root_span
    root_span.span.return_value = model_span
    model_span.generation.return_value = MagicMock()
    return client, trace, root_span


def _assert_trace_workflow_step(client, expected_step):
    call_kwargs = client.trace.call_args.kwargs
    assert call_kwargs["name"] == "LangGraph"
    assert expected_step in call_kwargs["tags"]
    assert call_kwargs["metadata"]["workflow_step"] == expected_step


@pytest.mark.parametrize(
    "step_fn,expected_tag,expected_model",
    [
        ("seed_route_entry_trace", "route_entry", "claude-haiku-4-5@20251001"),
        ("seed_generate_prd_trace", "generate_prd", "claude-opus-4-7"),
        ("seed_regenerate_prd_trace", "regenerate_prd", "claude-opus-4-7"),
        ("seed_generate_spec_trace", "generate_spec", "claude-opus-4-5@20251101"),
        ("seed_decompose_epics_trace", "decompose_epics", "claude-opus-4-5@20251101"),
        ("seed_generate_tasks_trace", "generate_tasks", "claude-opus-4-5@20251101"),
    ],
)
def test_planning_seeder_sets_correct_tag_and_model(
    step_fn, expected_tag, expected_model
):
    import seed as seed_mod

    fn = getattr(seed_mod, step_fn)
    client, trace, _ = _make_mock_client()
    t = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
    result_t = fn(client, "AISOS-1", "Add login feature", t, is_outlier=False)
    _assert_trace_workflow_step(client, expected_tag)
    assert result_t > t


def test_planning_seeder_returns_later_time():
    from seed import seed_generate_prd_trace

    client, _, _ = _make_mock_client()
    t = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
    result_t = seed_generate_prd_trace(client, "AISOS-1", "Test", t)
    assert result_t > t


# ── Feature execution step seeders ───────────────────────────────────────────


@pytest.mark.parametrize(
    "step_fn,expected_tag,expected_model",
    [
        ("seed_implement_task_trace", "implement_task", "claude-sonnet-4-6"),
        ("seed_local_review_trace", "local_review", "gemini-2.5-pro"),
        ("seed_create_pr_trace", "create_pr", "claude-haiku-4-5@20251001"),
        ("seed_ci_evaluator_trace", "ci_evaluator", "claude-haiku-4-5@20251001"),
        ("seed_attempt_ci_fix_trace", "attempt_ci_fix", "claude-haiku-4-5@20251001"),
        ("seed_human_review_gate_trace", "human_review_gate", "gemini-2.5-pro"),
        (
            "seed_aggregate_feature_status_trace",
            "aggregate_feature_status",
            "claude-haiku-4-5@20251001",
        ),
    ],
)
def test_execution_seeder_sets_correct_tag_and_returns_later_time(
    step_fn, expected_tag, expected_model
):
    import seed as seed_mod

    fn = getattr(seed_mod, step_fn)
    client, _, _ = _make_mock_client()
    t = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
    result_t = fn(client, "AISOS-1", "Add login feature", t, is_outlier=False)
    _assert_trace_workflow_step(client, expected_tag)
    assert result_t > t


# ── Bug step seeders ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "step_fn,expected_tag,expected_model",
    [
        ("seed_analyze_bug_trace", "analyze_bug", "claude-opus-4-7"),
        ("seed_implement_bug_fix_trace", "implement_bug_fix", "claude-sonnet-4-6"),
    ],
)
def test_bug_seeder_sets_correct_tag_and_returns_later_time(
    step_fn, expected_tag, expected_model
):
    import seed as seed_mod

    fn = getattr(seed_mod, step_fn)
    client, _, _ = _make_mock_client()
    t = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
    result_t = fn(client, "AISOS-1", "Login fails on mobile", t, is_outlier=False)
    _assert_trace_workflow_step(client, expected_tag)
    assert result_t > t


# ── Ticket orchestrators ──────────────────────────────────────────────────────


def test_seed_feature_ticket_creates_correct_number_of_traces():
    from seed import seed_feature_ticket

    client, _, _ = _make_mock_client()
    t = datetime(2024, 6, 1, tzinfo=timezone.utc)
    trace_ids, cost, latency = seed_feature_ticket(
        client, "AISOS-1", "Add login feature", t, is_outlier=False
    )
    # At minimum: route_entry + generate_prd + generate_spec + decompose_epics +
    # generate_tasks + implement_task + local_review + create_pr +
    # ci_evaluator + human_review_gate + aggregate_feature_status = 11
    assert client.trace.call_count >= 11
    assert isinstance(trace_ids, list)


def test_seed_bug_ticket_creates_correct_number_of_traces():
    from seed import seed_bug_ticket

    client, _, _ = _make_mock_client()
    t = datetime(2024, 6, 1, tzinfo=timezone.utc)
    trace_ids, cost, latency = seed_bug_ticket(
        client, "AISOS-1", "Login fails", t, is_outlier=False
    )
    # At minimum: route_entry + analyze_bug + implement_bug_fix + local_review +
    # create_pr + ci_evaluator + human_review_gate = 7
    assert client.trace.call_count >= 7


def test_seed_feature_ticket_30pct_regenerate_prd(monkeypatch):
    """With random.random always < 0.3, regenerate_prd should be included."""
    from seed import seed_feature_ticket

    monkeypatch.setattr("random.random", lambda: 0.1)
    client, _, _ = _make_mock_client()
    t = datetime(2024, 6, 1, tzinfo=timezone.utc)
    trace_ids, cost, latency = seed_feature_ticket(
        client, "AISOS-1", "test", t, is_outlier=False
    )
    tags_seen = [c.kwargs["tags"][0] for c in client.trace.call_args_list]
    assert "regenerate_prd" in tags_seen


# ── Ticket type tags ──────────────────────────────────────────────────────────


class TestTicketTypeTags:
    """Verify that traces include the ticket_type tag."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        trace = MagicMock()
        root_span = MagicMock()
        model_span = MagicMock()
        client.trace.return_value = trace
        trace.span.return_value = root_span
        root_span.span.return_value = model_span
        model_span.generation.return_value = MagicMock()
        return client

    def test_seed_trace_includes_feature_tag(self, mock_client):
        _seed_trace(
            mock_client,
            session_id="AISOS-1",
            workflow_step="generate_prd",
            model=MODEL_DEEP_RESEARCH,
            prompt="test",
            output_content="test output",
            base_time=datetime.now(),
            category="deep_research",
            tool_calls_per_cycle=[[("read_file", {}, {})]],
            ticket_type="feature",
        )
        call_kwargs = mock_client.trace.call_args[1]
        assert "feature" in call_kwargs["tags"]
        assert "generate_prd" in call_kwargs["tags"]

    def test_seed_trace_includes_bug_tag(self, mock_client):
        _seed_trace(
            mock_client,
            session_id="AISOS-51",
            workflow_step="analyze_bug",
            model=MODEL_DEEP_RESEARCH,
            prompt="test",
            output_content="test output",
            base_time=datetime.now(),
            category="deep_research",
            tool_calls_per_cycle=[[("read_file", {}, {})]],
            ticket_type="bug",
        )
        call_kwargs = mock_client.trace.call_args[1]
        assert "bug" in call_kwargs["tags"]

    def test_seed_trace_includes_ticket_type_in_metadata(self, mock_client):
        _seed_trace(
            mock_client,
            session_id="AISOS-1",
            workflow_step="generate_prd",
            model=MODEL_DEEP_RESEARCH,
            prompt="test",
            output_content="test output",
            base_time=datetime.now(),
            category="deep_research",
            tool_calls_per_cycle=[[("read_file", {}, {})]],
            ticket_type="feature",
        )
        call_kwargs = mock_client.trace.call_args[1]
        assert call_kwargs["metadata"]["ticket_type"] == "feature"

    def test_seed_trace_includes_project_id_in_tags(self, mock_client):
        _seed_trace(
            mock_client,
            session_id="OSASINFRA-1",
            workflow_step="generate_prd",
            model=MODEL_DEEP_RESEARCH,
            prompt="test",
            output_content="test output",
            base_time=datetime.now(),
            category="deep_research",
            tool_calls_per_cycle=[[("read_file", {}, {})]],
            ticket_type="feature",
            project_id="OSASINFRA",
        )
        call_kwargs = mock_client.trace.call_args[1]
        assert "OSASINFRA" in call_kwargs["tags"]

    def test_seed_trace_includes_project_id_in_metadata(self, mock_client):
        _seed_trace(
            mock_client,
            session_id="OSASINFRA-1",
            workflow_step="generate_prd",
            model=MODEL_DEEP_RESEARCH,
            prompt="test",
            output_content="test output",
            base_time=datetime.now(),
            category="deep_research",
            tool_calls_per_cycle=[[("read_file", {}, {})]],
            ticket_type="feature",
            project_id="OSASINFRA",
        )
        call_kwargs = mock_client.trace.call_args[1]
        assert call_kwargs["metadata"]["project_id"] == "OSASINFRA"


def test_implement_task_trace_includes_project_id_in_tags():
    from seed import seed_implement_task_trace

    client, _, _ = _make_mock_client()
    t = datetime(2024, 6, 1, tzinfo=timezone.utc)
    seed_implement_task_trace(
        client, "OSASINFRA-1", "Test feature", t, project_id="OSASINFRA"
    )
    call_kwargs = client.trace.call_args_list[0][1]
    assert "OSASINFRA" in call_kwargs["tags"]
    assert call_kwargs["metadata"]["project_id"] == "OSASINFRA"


def test_implement_bug_fix_trace_includes_project_id_in_tags():
    from seed import seed_implement_bug_fix_trace

    client, _, _ = _make_mock_client()
    t = datetime(2024, 6, 1, tzinfo=timezone.utc)
    seed_implement_bug_fix_trace(
        client, "OSPA-26", "Login fails on mobile", t, project_id="OSPA"
    )
    call_kwargs = client.trace.call_args_list[0][1]
    assert "OSPA" in call_kwargs["tags"]
    assert call_kwargs["metadata"]["project_id"] == "OSPA"


# ── Seed output sidecar ───────────────────────────────────────────────────────


class TestSeedOutput:
    """Verify that seed_output.json is written with correct structure."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        trace = MagicMock()
        root_span = MagicMock()
        model_span = MagicMock()
        client.trace.return_value = trace
        trace.span.return_value = root_span
        root_span.span.return_value = model_span
        model_span.generation.return_value = MagicMock()
        return client

    def test_output_path_points_to_devtools(self):
        from seed import OUTPUT_PATH

        assert OUTPUT_PATH.name == "seed_output.json"
        assert OUTPUT_PATH.parent.name == "devtools"

    def test_feature_ticket_returns_trace_ids(self, mock_client):
        # trace.id needs to be a string for JSON serialization
        mock_client.trace.return_value.id = "mock-trace-id-feature"
        trace_ids, cost, latency = seed_feature_ticket(
            mock_client, "AISOS-1", "Test feature", datetime.now(), is_outlier=False
        )
        assert isinstance(trace_ids, list)
        assert len(trace_ids) > 0
        assert all(isinstance(tid, str) for tid in trace_ids)

    def test_bug_ticket_returns_trace_ids(self, mock_client):
        mock_client.trace.return_value.id = "mock-trace-id-bug"
        trace_ids, cost, latency = seed_bug_ticket(
            mock_client, "AISOS-51", "Test bug", datetime.now(), is_outlier=False
        )
        assert isinstance(trace_ids, list)
        assert len(trace_ids) > 0
        assert all(isinstance(tid, str) for tid in trace_ids)


def test_compute_cost_basic():
    # claude-haiku: input=0.000001, output=0.000005
    cost = _compute_cost("claude-haiku-4-5@20251001", 1_000_000, 100_000)
    assert cost == pytest.approx(1.0 + 0.5, rel=0.01)  # $1 input + $0.50 output


def test_feature_ticket_cost_is_positive():
    client, _, _ = _make_mock_client()
    t = datetime(2024, 6, 1, tzinfo=timezone.utc)
    trace_ids, cost, latency = seed_feature_ticket(
        client, "OSASINFRA-1", "Test", t, is_outlier=False
    )
    assert cost > 0


def test_feature_ticket_latency_is_positive():
    client, _, _ = _make_mock_client()
    t = datetime(2024, 6, 1, tzinfo=timezone.utc)
    trace_ids, cost, latency = seed_feature_ticket(
        client, "OSASINFRA-1", "Test", t, is_outlier=False
    )
    assert latency > 0
