#!/usr/bin/env python
"""Seed the local Langfuse instance with ~485 forge SDLC workflow traces."""

import json
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from faker import Faker
from langfuse import Langfuse

_env_path = Path(__file__).parents[2] / ".env"
if _env_path.exists():
    load_dotenv(_env_path)


@dataclass
class Config:
    public_key: str
    secret_key: str
    host: str
    clickhouse_url: str


def load_config() -> Config:
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    if not public_key:
        raise ValueError("LANGFUSE_PUBLIC_KEY environment variable is required")
    if not secret_key:
        raise ValueError("LANGFUSE_SECRET_KEY environment variable is required")
    port = os.environ.get("LANGFUSE_PORT", "3000")
    ch_port = os.environ.get("CLICKHOUSE_HTTP_PORT", "8124")
    ch_user = os.environ.get("CLICKHOUSE_USER", "clickhouse")
    ch_pass = os.environ.get("CLICKHOUSE_PASSWORD", "clickhouse")
    ch_db = os.environ.get("CLICKHOUSE_DATABASE", "default")
    return Config(
        public_key=public_key,
        secret_key=secret_key,
        host=f"http://localhost:{port}",
        clickhouse_url=f"http://localhost:{ch_port}/?user={ch_user}&password={ch_pass}&database={ch_db}",
    )


def check_connectivity(config: Config) -> None:
    import urllib.request

    try:
        urllib.request.urlopen(config.host, timeout=5)
    except Exception as exc:
        print(f"ERROR: Cannot reach Langfuse at {config.host}: {exc}", file=sys.stderr)
        raise SystemExit(1)


# Predecessor-based pricing for newer models not yet in Langfuse's table.
# Claude Opus 4.x → claude-3-opus-20240229 pricing
# Claude Sonnet 4.x → claude-3.5-sonnet-20241022 pricing
# Claude Haiku 4.x → claude-3-5-haiku-20241022 pricing
# Gemini 2.5 Pro → actual published Vertex AI pricing
MODEL_PRICING: dict[str, dict] = {
    "claude-opus-4-7": {
        "input_price": 0.000015,
        "output_price": 0.000075,
    },
    "claude-opus-4-5@20251101": {
        "input_price": 0.000015,
        "output_price": 0.000075,
    },
    "claude-sonnet-4-6": {
        "input_price": 0.000003,
        "output_price": 0.000015,
    },
    "gemini-2.5-pro": {
        "input_price": 0.00000125,
        "output_price": 0.00001,
    },
    "claude-haiku-4-5@20251001": {
        "input_price": 0.000001,
        "output_price": 0.000005,
    },
}


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = MODEL_PRICING[model]
    return input_tokens * prices["input_price"] + output_tokens * prices["output_price"]


def _ch_query(clickhouse_url: str, sql: str) -> None:
    """Execute a SQL statement against ClickHouse via the HTTP API."""
    import urllib.request

    req = urllib.request.Request(
        clickhouse_url,
        data=sql.encode(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def cleanup_seeded_data(config: Config) -> None:
    """Delete all seeded traces and their observations/scores from ClickHouse."""
    url = config.clickhouse_url
    project_list = ", ".join(f"'{p}'" for p in PROJECTS)
    _ch_query(
        url,
        f"DELETE FROM observations WHERE trace_id IN (SELECT id FROM traces WHERE hasAny(tags, [{project_list}]))",
    )
    _ch_query(
        url,
        f"DELETE FROM scores WHERE trace_id IN (SELECT id FROM traces WHERE hasAny(tags, [{project_list}]))",
    )
    _ch_query(url, f"DELETE FROM traces WHERE hasAny(tags, [{project_list}])")


# ── Distribution helpers ──────────────────────────────────────────────────────

# Log-normal parameters (mu, sigma) per step category, for (input, output) tokens.
# Normal draws stay in the left bulk; outlier draws shift mu rightward.
_TOKEN_PARAMS: dict[str, dict] = {
    "routing": {"mu_in": 12.5, "sigma_in": 0.5, "mu_out": 7.5, "sigma_out": 0.6},
    "deep_research": {"mu_in": 13.8, "sigma_in": 0.6, "mu_out": 10.5, "sigma_out": 0.6},
    "planning": {"mu_in": 13.2, "sigma_in": 0.5, "mu_out": 10.0, "sigma_out": 0.6},
    "code_writing": {"mu_in": 14.2, "sigma_in": 0.6, "mu_out": 10.8, "sigma_out": 0.6},
    "code_review": {"mu_in": 13.0, "sigma_in": 0.5, "mu_out": 9.5, "sigma_out": 0.6},
}
_OUTLIER_MU_BOOST = 2.0  # shift mu right for outlier traces


def sample_tokens(category: str, is_outlier: bool) -> tuple[int, int]:
    """Return (input_tokens, output_tokens) from a log-normal distribution."""
    p = _TOKEN_PARAMS[category]
    boost = _OUTLIER_MU_BOOST if is_outlier else 0.0
    inp = int(random.lognormvariate(p["mu_in"] + boost, p["sigma_in"]))
    out = int(random.lognormvariate(p["mu_out"] + boost, p["sigma_out"]))
    return max(10_000, inp), max(100, out)


# Log-normal parameters for trace duration in seconds.
_DURATION_PARAMS: dict[str, dict] = {
    "routing": {"mu": 2.5, "sigma": 0.5},
    "deep_research": {"mu": 4.5, "sigma": 0.6},
    "planning": {"mu": 4.0, "sigma": 0.5},
    "code_writing": {"mu": 5.5, "sigma": 0.6},
    "code_review": {"mu": 4.2, "sigma": 0.5},
}
_OUTLIER_DURATION_BOOST = 2.0


def sample_duration(category: str, is_outlier: bool) -> timedelta:
    """Return a trace/observation duration from a log-normal distribution."""
    p = _DURATION_PARAMS[category]
    boost = _OUTLIER_DURATION_BOOST if is_outlier else 0.0
    secs = random.lognormvariate(p["mu"] + boost, p["sigma"])
    return timedelta(seconds=max(1.0, secs))


# Per-step extra cycle probabilities. Each entry is (max_extra, p_per_cycle).
# Extra cycles are added geometrically: roll p_per_cycle until failure or max reached.
_EXTRA_CYCLE_PARAMS: dict[str, tuple[int, float]] = {
    "generate_spec": (3, 0.20),
    "implement_task": (4, 0.25),
    "implement_bug_fix": (3, 0.25),
    "local_review": (2, 0.15),
    "human_review_gate": (3, 0.20),
    "attempt_ci_fix": (2, 0.30),
}
_OUTLIER_P_BOOST = 0.20


def extra_cycles(step: str, is_outlier: bool) -> int:
    """Return additional model->tools cycles for iteration-heavy steps."""
    if step not in _EXTRA_CYCLE_PARAMS:
        return 0
    max_extra, p = _EXTRA_CYCLE_PARAMS[step]
    if is_outlier:
        p = min(0.9, p + _OUTLIER_P_BOOST)
    count = 0
    while count < max_extra and random.random() < p:
        count += 1
    return count


def random_past_datetime(days: int = 30) -> datetime:
    offset = timedelta(
        days=random.randint(0, days),
        hours=random.randint(0, 23),
        minutes=random.randint(0, 59),
    )
    return datetime.now(tz=timezone.utc) - offset


# ── Fake content ──────────────────────────────────────────────────────────────

fake = Faker()

_FEATURE_SUMMARIES = [
    "Add multi-factor authentication support",
    "Implement rate limiting for API endpoints",
    "Build automated report generation pipeline",
    "Add real-time collaboration features",
    "Integrate third-party payment gateway",
    "Implement advanced search and filtering",
    "Add dark mode support across the platform",
    "Build audit trail for compliance tracking",
    "Implement bulk data import and export",
    "Add webhook notification system",
    "Migrate legacy authentication to OAuth2",
    "Build self-service onboarding flow",
    "Add role-based access control",
    "Implement data retention policies",
    "Build API usage analytics dashboard",
    "Add single sign-on (SSO) support",
    "Implement GraphQL API layer",
]

_BUG_SUMMARIES = [
    "Login fails intermittently on mobile devices",
    "Dashboard not refreshing after data updates",
    "File uploads silently fail for large files",
    "Search results show stale cached data",
    "Email notifications sent with wrong timezone",
    "Pagination breaks on last page of results",
    "API rate limit headers missing from responses",
    "Password reset link expires too quickly",
    "CSV export truncates fields containing commas",
    "Session timeout not honored on Safari",
    "OAuth token refresh fails under load",
    "Webhook delivery retries not respecting backoff",
    "Audit log missing entries for bulk operations",
    "Memory leak in background job processor",
    "Race condition in concurrent file uploads",
]

_TEAM_MEMBERS = [
    "alice@example.com",
    "bob@example.com",
    "charlie@example.com",
    "diana@example.com",
    "evan@example.com",
    "fiona@example.com",
]


def make_ticket_key(prefix: str, num: int) -> str:
    return f"{prefix}-{num}"


def make_ticket_summary(workflow_type: str) -> str:
    pool = _BUG_SUMMARIES if workflow_type == "bug" else _FEATURE_SUMMARIES
    return random.choice(pool)


def make_user_id() -> str:
    return random.choice(_TEAM_MEMBERS)


def make_prd_prompt(ticket_key: str, summary: str) -> str:
    project_key = ticket_key.split("-")[0]
    return (
        f"Please create a Product Requirements Document from the following "
        f"raw requirements:\n\n{summary}\n\n"
        f"Additional context:\n{{'ticket_key': '{ticket_key}', "
        f"'summary': '{summary}', 'project_key': '{project_key}'}}\n\n"
        f"Generate a comprehensive, well-structured PRD following the instructions provided.\n\n"
        f"IMPORTANT: Do all research silently using your tools. Your entire response must be "
        f"ONLY the PRD document — no preamble, no explanation of what you found, no narration "
        f"of your research process. Start directly with the document title."
    )


def make_spec_prompt(ticket_key: str, summary: str, prd_content: str) -> str:
    return (
        f"Please create a detailed behavioral specification from the following PRD:\n\n"
        f"{prd_content[:2000]}\n\n"
        f"Ticket: {ticket_key} — {summary}\n\n"
        f"Generate a complete specification with acceptance criteria in Given/When/Then format."
    )


def make_rca_prompt(ticket_key: str, summary: str) -> str:
    return (
        f"Please analyze the following bug report and produce a detailed root cause analysis:\n\n"
        f"Ticket: {ticket_key}\nSummary: {summary}\n\n"
        f"Investigate the root cause, affected code paths, and propose a fix."
    )


def make_implement_prompt(ticket_key: str, task_desc: str) -> str:
    return (
        f"Implement the following task for ticket {ticket_key}:\n\n{task_desc}\n\n"
        f"Search the codebase for relevant files, implement the changes, and ensure tests pass."
    )


def make_review_prompt(ticket_key: str, summary: str) -> str:
    return (
        f"Review the pull request for ticket {ticket_key}: {summary}\n\n"
        f"Check the implementation against the specification, identify any issues, "
        f"and provide a detailed review."
    )


def make_prd_content(summary: str) -> str:
    return f"""# Product Requirements Document: {summary}

## Overview
{fake.paragraph(nb_sentences=3)}

## Problem Statement
{fake.paragraph(nb_sentences=4)}

## Goals
- {fake.sentence()}
- {fake.sentence()}
- {fake.sentence()}

## User Stories
1. As a {fake.job()}, I want to {fake.sentence().lower()} so that {fake.sentence().lower()}
2. As a {fake.job()}, I want to {fake.sentence().lower()} so that {fake.sentence().lower()}

## Success Criteria
- {fake.sentence()}
- {fake.sentence()}

## Out of Scope
- {fake.sentence()}
"""


def make_spec_content(summary: str) -> str:
    return f"""# Specification: {summary}

## Acceptance Criteria

**Scenario 1: Happy path**
- Given {fake.sentence().lower()}
- When {fake.sentence().lower()}
- Then {fake.sentence().lower()}

**Scenario 2: Error handling**
- Given {fake.sentence().lower()}
- When {fake.sentence().lower()}
- Then {fake.sentence().lower()}

## Non-Functional Requirements
- Response time: < 200ms at 95th percentile
- Availability: 99.9% uptime
"""


def make_rca_content(summary: str) -> str:
    return f"""# Root Cause Analysis: {summary}

## Root Cause
{fake.paragraph(nb_sentences=3)}

## Affected Areas
- `src/{fake.word()}/{fake.word()}.py`
- `src/{fake.word()}/handlers.py`

## Proposed Fix
{fake.paragraph(nb_sentences=2)}

## Testing Approach
{fake.sentence()}
"""


# ── LangGraph builder helpers ─────────────────────────────────────────────────

_CLAUDE_MODELS = {
    "claude-opus-4-7",
    "claude-opus-4-5@20251101",
    "claude-sonnet-4-6",
    "claude-haiku-4-5@20251001",
}


def _generation_name(model: str) -> str:
    return "ChatAnthropicVertex" if model in _CLAUDE_MODELS else "ChatVertexAI"


def build_langgraph_root(trace, t: datetime, prompt: str) -> tuple:
    """Create root LangGraph CHAIN span with middleware AGENT children.

    Returns (root_span, t_after_middleware).
    """
    root = trace.span(
        name="LangGraph",
        start_time=t,
        input={"messages": [{"role": "user", "content": prompt}]},
    )
    t_mw = t + timedelta(milliseconds=random.uniform(0.5, 5))
    root.span(
        name="SkillsMiddleware.before_agent",
        start_time=t,
        end_time=t_mw,
        input={},
        output={},
    )
    t_mw2 = t_mw + timedelta(milliseconds=random.uniform(0.1, 1))
    root.span(
        name="PatchToolCallsMiddleware.before_agent",
        start_time=t_mw,
        end_time=t_mw2,
        input={},
        output={},
    )
    return root, t_mw2


def add_model_cycle(
    root_span,
    t: datetime,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    tool_calls: list[tuple[str, dict, dict]],
    gen_duration: timedelta | None = None,
) -> datetime:
    """Add one model→generation→tools iteration to root_span. Returns end time."""
    if gen_duration is None:
        gen_duration = timedelta(seconds=random.lognormvariate(2.5, 0.5))

    model_span = root_span.span(
        name="model",
        start_time=t,
        input={},
        output={},
    )
    prices = MODEL_PRICING[model]
    input_cost = prompt_tokens * prices["input_price"]
    output_cost = completion_tokens * prices["output_price"]
    model_span.generation(
        name=_generation_name(model),
        model=model,
        model_parameters={"max_tokens": 16384},
        start_time=t,
        end_time=t + gen_duration,
        input=[{"role": "user", "content": "<context>"}],
        output="<assistant response>",
        usage={
            "input": prompt_tokens,
            "output": completion_tokens,
            "total": prompt_tokens + completion_tokens,
            "input_cost": input_cost,
            "output_cost": output_cost,
            "total_cost": input_cost + output_cost,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    )
    t = t + gen_duration
    model_span.update(end_time=t)

    # TodoListMiddleware (near-instant, always present)
    t_todo = t + timedelta(milliseconds=random.uniform(0.1, 2))
    root_span.span(
        name="TodoListMiddleware.after_model",
        start_time=t,
        end_time=t_todo,
        input={},
        output=None,
    )
    t = t_todo

    # One sibling tools CHAIN per tool call (mirrors real parallel execution)
    for tool_name, tool_input, tool_output in tool_calls:
        tool_dur = timedelta(seconds=random.lognormvariate(1.0, 0.6))
        tools_chain = root_span.span(
            name="tools",
            start_time=t,
            end_time=t + tool_dur,
            input={},
            output={},
        )
        tools_chain.span(
            name=tool_name,
            start_time=t,
            end_time=t + tool_dur,
            input=tool_input,
            output=tool_output,
        )
        t = t + tool_dur

    return t


def add_subagent(
    root_span,
    t: datetime,
    model: str,
    task_prompt: str,
    tool_calls: list[tuple[str, dict, dict]],
    cycles: int,
    is_outlier: bool,
) -> tuple[datetime, float]:
    """Add task(TOOL) → general-purpose(CHAIN) sub-agent to root_span."""
    subagent_dur = sample_duration("code_writing", is_outlier)

    tools_chain = root_span.span(
        name="tools",
        start_time=t,
        end_time=t + subagent_dur,
        input={},
        output={},
    )
    task_tool = tools_chain.span(
        name="task",
        start_time=t,
        end_time=t + subagent_dur,
        input={"prompt": task_prompt},
        output={},
    )
    gp_chain = task_tool.span(
        name="general-purpose",
        start_time=t,
        end_time=t + subagent_dur,
        input={},
        output={},
    )

    sub_t = t
    tools_per_cycle = tool_calls or [
        ("search_code", {}, {}),
        ("get_file_contents", {}, {}),
    ]
    inp, out = sample_tokens("code_writing", is_outlier)
    subagent_cost = _compute_cost(model, inp, out)
    tokens_per_cycle = (inp // max(cycles, 1), out // max(cycles, 1))

    for _ in range(cycles):
        sub_t = add_model_cycle(
            gp_chain,
            sub_t,
            model=model,
            prompt_tokens=tokens_per_cycle[0],
            completion_tokens=tokens_per_cycle[1],
            tool_calls=tools_per_cycle,
        )

    gp_chain.update(end_time=sub_t)
    task_tool.update(end_time=sub_t)
    tools_chain.update(end_time=sub_t)
    return sub_t, subagent_cost


# ── Output path ───────────────────────────────────────────────────────────────

OUTPUT_PATH = Path(__file__).parent.parent / "seed_output.json"

# ── Model assignment constants ─────────────────────────────────────────────────

MODEL_DEEP_RESEARCH = "claude-opus-4-7"
MODEL_PLANNING = "claude-opus-4-5@20251101"
MODEL_CODE_WRITING = "claude-sonnet-4-6"
MODEL_CODE_REVIEW = "gemini-2.5-pro"
MODEL_ROUTING = "claude-haiku-4-5@20251001"


# ── Feature planning step seeders ─────────────────────────────────────────────


def _seed_trace(
    client,
    session_id: str,
    workflow_step: str,
    model: str,
    prompt: str,
    output_content: str,
    base_time: datetime,
    category: str,
    tool_calls_per_cycle: list[list[tuple]],
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    """Generic trace builder used by all step seeders."""
    tags = [workflow_step]
    if ticket_type:
        tags.append(ticket_type)
    if project_id:
        tags.append(project_id)
    metadata = {"workflow_step": workflow_step}
    if ticket_type:
        metadata["ticket_type"] = ticket_type
    if project_id:
        metadata["project_id"] = project_id
    trace = client.trace(
        name="LangGraph",
        session_id=session_id,
        tags=tags,
        metadata=metadata,
        input={"messages": [{"role": "user", "content": prompt}]},
        output={"messages": [{"role": "assistant", "content": output_content}]},
        timestamp=base_time,
    )
    if _trace_ids is not None:
        _trace_ids.append(trace.id)
    root, t = build_langgraph_root(trace, base_time, prompt)
    trace_cost = 0.0
    n_cycles = len(tool_calls_per_cycle)
    for i, tools in enumerate(tool_calls_per_cycle):
        inp, out = sample_tokens(category, is_outlier)
        trace_cost += _compute_cost(model, inp, out)
        if i == n_cycles - 1:
            out = max(out, int(len(output_content) / 4))
        t = add_model_cycle(
            root,
            t,
            model=model,
            prompt_tokens=inp,
            completion_tokens=out,
            tool_calls=tools,
        )
    if _costs is not None:
        _costs.append(trace_cost)
    root.update(end_time=t)
    return t


def seed_route_entry_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    prompt = f"Route ticket {session_id}: {ticket_summary}"
    cycles = [
        [
            ("issue_read", {"issue_id": session_id}, {"summary": ticket_summary}),
            (
                "read_file",
                {"path": "/skills/router/SKILL.md"},
                {"content": "<routing skill>"},
            ),
        ],
    ]
    extra = extra_cycles("route_entry", is_outlier)
    cycles += [[("read_file", {"path": "/config"}, {})] for _ in range(extra)]
    return _seed_trace(
        client,
        session_id,
        "route_entry",
        MODEL_ROUTING,
        prompt,
        f"Routed to workflow for: {ticket_summary}",
        base_time,
        "routing",
        cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


def seed_generate_prd_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    prd = make_prd_content(ticket_summary)
    prompt = make_prd_prompt(session_id, ticket_summary)
    cycles = [
        [
            (
                "read_file",
                {"path": "/skills/generate-prd/SKILL.md"},
                {"content": "<skill>"},
            ),
            (
                "read_file",
                {"path": "/skills/generate-prd/INSTRUCTIONS.md"},
                {"content": "<instructions>"},
            ),
        ],
        [
            ("search_repositories", {"query": ticket_summary[:40]}, {"results": []}),
            ("search_issues", {"query": ticket_summary[:40]}, {"results": []}),
            ("issue_read", {"issue_id": session_id}, {"summary": ticket_summary}),
        ],
        [],
    ]
    extra = extra_cycles("generate_prd", is_outlier)
    cycles += [
        [("search_code", {"query": ticket_summary[:30]}, {})] for _ in range(extra)
    ]
    return _seed_trace(
        client,
        session_id,
        "generate_prd",
        MODEL_DEEP_RESEARCH,
        prompt,
        prd,
        base_time,
        "deep_research",
        cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


def seed_regenerate_prd_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    prd = make_prd_content(ticket_summary)
    prompt = f"Please revise the following PRD based on the feedback provided:\n\n{prd[:500]}\n\nFeedback: {fake.sentence()}"
    cycles = [
        [
            (
                "read_file",
                {"path": "/skills/generate-prd/SKILL.md"},
                {"content": "<skill>"},
            )
        ],
        [("search_code", {"query": ticket_summary[:30]}, {})],
        [],
    ]
    return _seed_trace(
        client,
        session_id,
        "regenerate_prd",
        MODEL_DEEP_RESEARCH,
        prompt,
        make_prd_content(ticket_summary),
        base_time,
        "deep_research",
        cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


def seed_generate_spec_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    prd = make_prd_content(ticket_summary)
    spec = make_spec_content(ticket_summary)
    prompt = make_spec_prompt(session_id, ticket_summary, prd)
    base_cycles = [
        [
            (
                "read_file",
                {"path": "/skills/generate-spec/SKILL.md"},
                {"content": "<skill>"},
            )
        ],
        [
            ("search_code", {"query": ticket_summary[:30]}, {}),
            ("search_issues", {"query": ticket_summary[:30]}, {}),
        ],
        [],
    ]
    extra = extra_cycles("generate_spec", is_outlier)
    base_cycles += [
        [("search_code", {"query": "spec iteration"}, {})] for _ in range(extra)
    ]
    return _seed_trace(
        client,
        session_id,
        "generate_spec",
        MODEL_PLANNING,
        prompt,
        spec,
        base_time,
        "planning",
        base_cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


def seed_decompose_epics_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    spec = make_spec_content(ticket_summary)
    prompt = f"Decompose the following specification into epics for ticket {session_id}:\n\n{spec[:1000]}"
    cycles = [
        [
            (
                "read_file",
                {"path": "/skills/decompose-epics/SKILL.md"},
                {"content": "<skill>"},
            )
        ],
        [
            ("search_repositories", {"query": "org"}, {}),
            ("search_code", {"query": ticket_summary[:30]}, {}),
        ],
        [],
    ]
    return _seed_trace(
        client,
        session_id,
        "decompose_epics",
        MODEL_PLANNING,
        prompt,
        f"# Epics for {ticket_summary}\n\n## Epic 1\n{fake.paragraph()}",
        base_time,
        "planning",
        cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


def seed_generate_tasks_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    prompt = f"Generate implementation tasks for ticket {session_id}: {ticket_summary}"
    cycles = [
        [
            (
                "read_file",
                {"path": "/skills/generate-tasks/SKILL.md"},
                {"content": "<skill>"},
            )
        ],
        [
            ("search_repositories", {"query": "org"}, {}),
            ("write_todos", {"todos": []}, {"result": "ok"}),
        ],
        [],
    ]
    return _seed_trace(
        client,
        session_id,
        "generate_tasks",
        MODEL_PLANNING,
        prompt,
        f"Tasks generated for {ticket_summary}",
        base_time,
        "planning",
        cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


# ── Feature execution step seeders ────────────────────────────────────────────


def seed_implement_task_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    task_desc = f"Implement: {ticket_summary}\n\n{fake.paragraph(nb_sentences=3)}"
    prompt = make_implement_prompt(session_id, task_desc)
    tags = ["implement_task"]
    if ticket_type:
        tags.append(ticket_type)
    if project_id:
        tags.append(project_id)
    metadata = {"workflow_step": "implement_task"}
    if ticket_type:
        metadata["ticket_type"] = ticket_type
    if project_id:
        metadata["project_id"] = project_id
    trace = client.trace(
        name="LangGraph",
        session_id=session_id,
        tags=tags,
        metadata=metadata,
        input={"messages": [{"role": "user", "content": prompt}]},
        output={
            "messages": [
                {
                    "role": "assistant",
                    "content": f"Implementation complete for {session_id}",
                }
            ]
        },
        timestamp=base_time,
    )
    if _trace_ids is not None:
        _trace_ids.append(trace.id)
    root, t = build_langgraph_root(trace, base_time, prompt)
    inp, out = sample_tokens("code_writing", is_outlier)
    direct_cost = _compute_cost(MODEL_CODE_WRITING, inp // 3, out // 10)
    t = add_model_cycle(
        root,
        t,
        model=MODEL_CODE_WRITING,
        prompt_tokens=inp // 3,
        completion_tokens=out // 10,
        tool_calls=[("write_todos", {"todos": [task_desc[:50]]}, {"result": "ok"})],
    )
    cycles = 3 + extra_cycles("implement_task", is_outlier)
    t, sa_cost = add_subagent(
        root,
        t,
        model=MODEL_CODE_WRITING,
        task_prompt=task_desc,
        tool_calls=[("search_code", {}, {}), ("get_file_contents", {}, {})],
        cycles=cycles,
        is_outlier=is_outlier,
    )
    if _costs is not None:
        _costs.append(direct_cost + sa_cost)
    root.update(end_time=t)
    return t


def seed_local_review_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    prompt = make_review_prompt(session_id, ticket_summary)
    base_cycles = [
        [
            ("get_file_contents", {"path": "src/main.py"}, {"content": fake.text()}),
            ("search_code", {"query": ticket_summary[:30]}, {}),
        ],
        [
            (
                "get_file_contents",
                {"path": "tests/test_main.py"},
                {"content": fake.text()},
            )
        ],
        [],
    ]
    extra = extra_cycles("local_review", is_outlier)
    base_cycles += [[("search_code", {"query": "review"}, {})] for _ in range(extra)]
    return _seed_trace(
        client,
        session_id,
        "local_review",
        MODEL_CODE_REVIEW,
        prompt,
        f"Code review complete for {session_id}. {fake.sentence()}",
        base_time,
        "code_review",
        base_cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


def seed_create_pr_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    pr_num = random.randint(100, 999)
    prompt = f"Create a pull request for ticket {session_id}: {ticket_summary}"
    cycles = [
        [("get_file_contents", {"path": "CHANGELOG.md"}, {"content": "<changelog>"})],
        [
            (
                "create_pull_request",
                {
                    "title": f"[{session_id}] {ticket_summary}",
                    "base": "main",
                    "head": f"forge/{session_id.lower()}",
                },
                {"url": f"https://github.com/org/repo/pull/{pr_num}", "number": pr_num},
            )
        ],
    ]
    return _seed_trace(
        client,
        session_id,
        "create_pr",
        MODEL_ROUTING,
        prompt,
        f"PR #{pr_num} created: https://github.com/org/repo/pull/{pr_num}",
        base_time,
        "routing",
        cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


def seed_ci_evaluator_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    ci_passed = random.random() > 0.25
    prompt = f"Evaluate CI results for ticket {session_id}"
    cycles = [
        [
            (
                "get_check_runs",
                {"owner": "org", "repo": "repo", "ref": "HEAD"},
                {
                    "check_runs": [
                        {
                            "name": "ci",
                            "conclusion": "success" if ci_passed else "failure",
                        }
                    ]
                },
            )
        ],
    ]
    return _seed_trace(
        client,
        session_id,
        "ci_evaluator",
        MODEL_ROUTING,
        prompt,
        f"CI {'passed' if ci_passed else 'failed'} for {session_id}",
        base_time,
        "routing",
        cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


def seed_attempt_ci_fix_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    prompt = (
        f"CI failed for ticket {session_id}. Analyze the failure and implement a fix."
    )
    base_cycles = [
        [
            (
                "get_check_runs",
                {},
                {
                    "check_runs": [
                        {"conclusion": "failure", "output": {"text": fake.sentence()}}
                    ]
                },
            ),
            ("search_code", {"query": "test failure"}, {}),
        ],
        [("get_file_contents", {"path": "src/main.py"}, {"content": fake.text()})],
        [],
    ]
    extra = extra_cycles("attempt_ci_fix", is_outlier)
    base_cycles += [[("search_code", {}, {})] for _ in range(extra)]
    return _seed_trace(
        client,
        session_id,
        "attempt_ci_fix",
        MODEL_ROUTING,
        prompt,
        f"CI fix applied for {session_id}. {fake.sentence()}",
        base_time,
        "routing",
        base_cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


def seed_human_review_gate_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    prompt = make_review_prompt(session_id, ticket_summary)
    base_cycles = [
        [
            (
                "read_file",
                {"path": "spec.md"},
                {"content": make_spec_content(ticket_summary)},
            ),
            ("get_file_contents", {"path": "src/main.py"}, {"content": fake.text()}),
        ],
        [
            ("search_code", {"query": ticket_summary[:30]}, {}),
            (
                "get_file_contents",
                {"path": "tests/test_main.py"},
                {"content": fake.text()},
            ),
        ],
        [],
    ]
    extra = extra_cycles("human_review_gate", is_outlier)
    base_cycles += [
        [("get_file_contents", {"path": f"src/file_{i}.py"}, {})] for i in range(extra)
    ]
    return _seed_trace(
        client,
        session_id,
        "human_review_gate",
        MODEL_CODE_REVIEW,
        prompt,
        f"Review approved for {session_id}. Ready to merge.",
        base_time,
        "code_review",
        base_cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


def seed_aggregate_feature_status_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    prompt = f"Aggregate and update status for feature ticket {session_id}"
    cycles = [
        [("issue_read", {"issue_id": session_id}, {"status": "in progress"})],
    ]
    return _seed_trace(
        client,
        session_id,
        "aggregate_feature_status",
        MODEL_ROUTING,
        prompt,
        f"Feature {session_id} marked complete.",
        base_time,
        "routing",
        cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


# ── Bug step seeders ──────────────────────────────────────────────────────────


def seed_analyze_bug_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    rca = make_rca_content(ticket_summary)
    prompt = make_rca_prompt(session_id, ticket_summary)
    base_cycles = [
        [
            (
                "read_file",
                {"path": "/skills/analyze-bug/SKILL.md"},
                {"content": "<skill>"},
            ),
            ("search_issues", {"query": ticket_summary[:40]}, {"results": []}),
        ],
        [
            ("search_code", {"query": ticket_summary[:30]}, {}),
            ("get_file_contents", {"path": "src/main.py"}, {"content": fake.text()}),
        ],
        [
            ("search_code", {"query": "error handler"}, {}),
            ("get_file_contents", {"path": "src/errors.py"}, {"content": fake.text()}),
        ],
        [],
    ]
    extra = extra_cycles("analyze_bug", is_outlier)
    base_cycles += [[("search_code", {}, {})] for _ in range(extra)]
    return _seed_trace(
        client,
        session_id,
        "analyze_bug",
        MODEL_DEEP_RESEARCH,
        prompt,
        rca,
        base_time,
        "deep_research",
        base_cycles,
        is_outlier,
        ticket_type=ticket_type,
        project_id=project_id,
        _trace_ids=_trace_ids,
        _costs=_costs,
    )


def seed_implement_bug_fix_trace(
    client,
    session_id: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    ticket_type: str = "",
    project_id: str = "",
    _trace_ids: list[str] | None = None,
    _costs: list[float] | None = None,
) -> datetime:
    rca = make_rca_content(ticket_summary)
    prompt = f"Implement the fix described in this RCA for ticket {session_id}:\n\n{rca[:500]}"
    tags = ["implement_bug_fix"]
    if ticket_type:
        tags.append(ticket_type)
    if project_id:
        tags.append(project_id)
    metadata = {"workflow_step": "implement_bug_fix"}
    if ticket_type:
        metadata["ticket_type"] = ticket_type
    if project_id:
        metadata["project_id"] = project_id
    trace = client.trace(
        name="LangGraph",
        session_id=session_id,
        tags=tags,
        metadata=metadata,
        input={"messages": [{"role": "user", "content": prompt}]},
        output={
            "messages": [
                {
                    "role": "assistant",
                    "content": f"Bug fix implemented for {session_id}",
                }
            ]
        },
        timestamp=base_time,
    )
    if _trace_ids is not None:
        _trace_ids.append(trace.id)
    root, t = build_langgraph_root(trace, base_time, prompt)
    inp, out = sample_tokens("code_writing", is_outlier)
    direct_cost = _compute_cost(MODEL_CODE_WRITING, inp // 3, out // 10)
    t = add_model_cycle(
        root,
        t,
        model=MODEL_CODE_WRITING,
        prompt_tokens=inp // 3,
        completion_tokens=out // 10,
        tool_calls=[
            (
                "write_todos",
                {"todos": [f"fix: {ticket_summary[:30]}"]},
                {"result": "ok"},
            )
        ],
    )
    cycles = 3 + extra_cycles("implement_bug_fix", is_outlier)
    t, sa_cost = add_subagent(
        root,
        t,
        model=MODEL_CODE_WRITING,
        task_prompt=prompt,
        tool_calls=[("search_code", {}, {}), ("get_file_contents", {}, {})],
        cycles=cycles,
        is_outlier=is_outlier,
    )
    if _costs is not None:
        _costs.append(direct_cost + sa_cost)
    root.update(end_time=t)
    return t


# ── Ticket orchestrators ──────────────────────────────────────────────────────


def _SHORT_GATE():
    return timedelta(hours=random.uniform(0.5, 4))


def _LONG_GATE():
    return timedelta(hours=random.uniform(4, 24))


def _REVIEW_GATE():
    return timedelta(hours=random.uniform(1, 8))


def seed_feature_ticket(
    client,
    ticket_key: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    project_id: str = "",
) -> tuple[list[str], float, float]:
    t = base_time
    tt = "feature"
    trace_ids: list[str] = []
    costs: list[float] = []
    machine_time: float = 0.0

    t0 = t
    t = seed_route_entry_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t0 = t
    t = seed_generate_prd_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    if random.random() < 0.30:
        t0 = t
        t = seed_regenerate_prd_trace(
            client,
            ticket_key,
            ticket_summary,
            t,
            is_outlier,
            ticket_type=tt,
            project_id=project_id,
            _trace_ids=trace_ids,
            _costs=costs,
        )
        machine_time += (t - t0).total_seconds()

    t += _SHORT_GATE()

    t0 = t
    t = seed_generate_spec_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t += _SHORT_GATE()

    t0 = t
    t = seed_decompose_epics_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t += _LONG_GATE()

    t0 = t
    t = seed_generate_tasks_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t += _LONG_GATE()

    t0 = t
    t = seed_implement_task_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t0 = t
    t = seed_local_review_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t0 = t
    t = seed_create_pr_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t0 = t
    t = seed_ci_evaluator_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    if is_outlier or random.random() < 0.30:
        t0 = t
        t = seed_attempt_ci_fix_trace(
            client,
            ticket_key,
            ticket_summary,
            t,
            is_outlier,
            ticket_type=tt,
            project_id=project_id,
            _trace_ids=trace_ids,
            _costs=costs,
        )
        machine_time += (t - t0).total_seconds()

    t += _REVIEW_GATE()

    t0 = t
    t = seed_human_review_gate_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t0 = t
    t = seed_aggregate_feature_status_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    return trace_ids, sum(costs), machine_time


def seed_bug_ticket(
    client,
    ticket_key: str,
    ticket_summary: str,
    base_time: datetime,
    is_outlier: bool = False,
    project_id: str = "",
) -> tuple[list[str], float, float]:
    t = base_time
    tt = "bug"
    trace_ids: list[str] = []
    costs: list[float] = []
    machine_time: float = 0.0

    t0 = t
    t = seed_route_entry_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t0 = t
    t = seed_analyze_bug_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t += _SHORT_GATE()

    t0 = t
    t = seed_implement_bug_fix_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t0 = t
    t = seed_local_review_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t0 = t
    t = seed_create_pr_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    t0 = t
    t = seed_ci_evaluator_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    if is_outlier or random.random() < 0.20:
        t0 = t
        t = seed_attempt_ci_fix_trace(
            client,
            ticket_key,
            ticket_summary,
            t,
            is_outlier,
            ticket_type=tt,
            project_id=project_id,
            _trace_ids=trace_ids,
            _costs=costs,
        )
        machine_time += (t - t0).total_seconds()

    t += _REVIEW_GATE()

    t0 = t
    t = seed_human_review_gate_trace(
        client,
        ticket_key,
        ticket_summary,
        t,
        is_outlier,
        ticket_type=tt,
        project_id=project_id,
        _trace_ids=trace_ids,
        _costs=costs,
    )
    machine_time += (t - t0).total_seconds()

    return trace_ids, sum(costs), machine_time


# ── Main orchestrator ─────────────────────────────────────────────────────────

PROJECTS = ["OSASINFRA", "OSPA"]
_N_FEATURES_PER_PROJECT = 25
_N_BUGS_PER_PROJECT = 50
_N_FEATURES = _N_FEATURES_PER_PROJECT * len(PROJECTS)
_N_BUGS = _N_BUGS_PER_PROJECT * len(PROJECTS)
_N_TOTAL = _N_FEATURES + _N_BUGS
_OUTLIER_RATE = 0.05


def main() -> None:
    config = load_config()
    check_connectivity(config)

    print("Cleaning up previously seeded data...")
    cleanup_seeded_data(config)

    client = Langfuse(
        public_key=config.public_key,
        secret_key=config.secret_key,
        host=config.host,
    )

    print("Verifying Langfuse credentials...")
    try:
        client.auth_check()
    except Exception as exc:
        print(
            f"ERROR: Langfuse authentication failed: {exc}\n"
            f"  Verify LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in your .env",
            file=sys.stderr,
        )
        raise SystemExit(1)

    all_issue_records: list[dict] = []
    total_tickets = _N_TOTAL

    print(
        f"Seeding {total_tickets} tickets "
        f"({_N_FEATURES_PER_PROJECT} features + {_N_BUGS_PER_PROJECT} bugs) "
        f"× {len(PROJECTS)} projects at {config.host} ..."
    )

    global_num = 1
    for project_id in PROJECTS:
        for i in range(_N_FEATURES_PER_PROJECT):
            ticket_num = i + 1
            ticket_key = make_ticket_key(project_id, ticket_num)
            summary = make_ticket_summary("feature")
            base_time = random_past_datetime(days=730)
            is_outlier = random.random() < _OUTLIER_RATE
            trace_ids, total_cost, total_latency_s = seed_feature_ticket(
                client,
                ticket_key,
                summary,
                base_time,
                is_outlier,
                project_id=project_id,
            )
            all_issue_records.append(
                {
                    "issue_id": ticket_key,
                    "project_id": project_id,
                    "ticket_type": "feature",
                    "is_outlier": is_outlier,
                    "base_time": base_time.isoformat().replace("+00:00", "Z"),
                    "trace_ids": trace_ids,
                    "total_cost": round(total_cost, 4),
                    "total_latency_s": round(total_latency_s, 1),
                }
            )
            outlier_tag = " [OUTLIER]" if is_outlier else ""
            print(
                f"  [{global_num:03d}/{total_tickets}] {ticket_key} feature{outlier_tag}: {summary[:60]}"
            )
            global_num += 1

        for i in range(_N_BUGS_PER_PROJECT):
            ticket_num = _N_FEATURES_PER_PROJECT + i + 1
            ticket_key = make_ticket_key(project_id, ticket_num)
            summary = make_ticket_summary("bug")
            base_time = random_past_datetime(days=730)
            is_outlier = random.random() < _OUTLIER_RATE
            trace_ids, total_cost, total_latency_s = seed_bug_ticket(
                client,
                ticket_key,
                summary,
                base_time,
                is_outlier,
                project_id=project_id,
            )
            all_issue_records.append(
                {
                    "issue_id": ticket_key,
                    "project_id": project_id,
                    "ticket_type": "bug",
                    "is_outlier": is_outlier,
                    "base_time": base_time.isoformat().replace("+00:00", "Z"),
                    "trace_ids": trace_ids,
                    "total_cost": round(total_cost, 4),
                    "total_latency_s": round(total_latency_s, 1),
                }
            )
            outlier_tag = " [OUTLIER]" if is_outlier else ""
            print(
                f"  [{global_num:03d}/{total_tickets}] {ticket_key} bug{outlier_tag}: {summary[:60]}"
            )
            global_num += 1

    client.flush()

    output = {
        "seeded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "projects": PROJECTS,
        "n_features": _N_FEATURES,
        "n_bugs": _N_BUGS,
        "n_features_per_project": _N_FEATURES_PER_PROJECT,
        "n_bugs_per_project": _N_BUGS_PER_PROJECT,
        "window_days": 730,
        "issues": all_issue_records,
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"\nDone. Seeded {total_tickets} tickets. Wrote seed output to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
