"""forge-observability-api — standalone FastAPI application."""

import logging
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import column, func, literal_column, select, text
from sqlalchemy.sql import Executable
from sqlalchemy.sql.expression import table as sa_table

from forge.observability.repository.repository import Repository, get_repository

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Forge Observability API",
    description="Observability API for Forge SDLC Orchestrator",
    version="0.1.0",
)

# TODO: Design authz layer and implement middleware

# ── Table / view references ─────────────────────────────────────────────────

_llm_traces = sa_table("bronze___llm_traces")
_ci_checks = sa_table("bronze___ci_checks")
_ticket_full_view = sa_table("silver___ticket_full_view")
_pr_with_llm_cost = sa_table("silver___pr_with_llm_cost")
_stage_performance = sa_table("silver___stage_performance")

# ── Dependency ─────────────────────────────────────────────────────────────


def _get_repo() -> Generator[Repository, None, None]:
    yield get_repository()


Repo = Annotated[Repository, Depends(_get_repo)]


# ── Utilities ──────────────────────────────────────────────────────────────


def _rows(repo: Repository, stmt: Executable) -> list[dict]:
    try:
        return repo.query(stmt)
    except Exception as exc:
        logger.exception("Database query failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _row(repo: Repository, stmt: Executable) -> dict:
    try:
        result = repo.query_one(stmt)
    except Exception as exc:
        logger.exception("Database query failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="No data found")
    return result


# ── Health ─────────────────────────────────────────────────────────────────


@app.get("/health", tags=["ops"])
def health(repo: Repo) -> dict:
    """Liveness check — also verifies database connectivity."""
    try:
        repo.query_one(text("SELECT 1"))
        return {"status": "ok", "store": "sqlalchemy"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ── Bronze — raw drill-down endpoints ──────────────────────────────────────


@app.get("/traces", tags=["bronze"])
def get_traces(
    repo: Repo,
    ticket_key: str | None = Query(None, description="Filter by JIRA ticket key"),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """Recent LLM traces. Use ?ticket_key=FOR-123 to drill down to one ticket."""
    stmt = (
        select(literal_column("*"))
        .select_from(_llm_traces)
        .order_by(column("timestamp").desc())
        .limit(limit)
    )
    if ticket_key:
        stmt = stmt.where(column("ticket_key") == ticket_key)
    return _rows(repo, stmt)


@app.get("/traces/summary", tags=["bronze"])
def get_traces_summary(repo: Repo, days: int = Query(7, ge=1, le=90)) -> list[dict]:
    """Aggregate latency and cost per trace name over the last N days."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(
            column("name"),
            func.count().label("trace_count"),
            func.round(func.avg(column("latency_ms")), 0).label("avg_latency_ms"),
            func.round(func.sum(column("total_cost")), 4).label("total_cost"),
        )
        .select_from(_llm_traces)
        .where(column("timestamp") >= cutoff)
        .group_by(column("name"))
        .order_by(func.count().desc())
    )
    return _rows(repo, stmt)


@app.get("/ci-checks", tags=["bronze"])
def get_ci_checks(
    repo: Repo,
    repo_name: str | None = Query(
        None, alias="repo", description="Filter by GitHub repo (owner/repo)"
    ),
    conclusion: str | None = Query(None, description="Filter by conclusion (success/failure)"),
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict]:
    """Recent CI check runs from bronze___ci_checks."""
    stmt = (
        select(literal_column("*"))
        .select_from(_ci_checks)
        .order_by(column("completed_at").desc())
        .limit(limit)
    )
    if repo_name:
        stmt = stmt.where(column("repo") == repo_name)
    if conclusion:
        stmt = stmt.where(column("conclusion") == conclusion)
    return _rows(repo, stmt)


# ── Silver — cross-source aggregated endpoints ─────────────────────────────


@app.get("/tickets/{ticket_key}/summary", tags=["silver"])
def get_ticket_summary(repo: Repo, ticket_key: str) -> dict:
    """Cross-source ticket summary: LLM cost + PR metrics + human interactions.

    Queries silver___ticket_full_view which joins all four bronze tables.
    """
    stmt = (
        select(literal_column("*"))
        .select_from(_ticket_full_view)
        .where(column("ticket_key") == ticket_key)
    )
    return _row(repo, stmt)


@app.get("/insights/workflows", tags=["silver"])
def get_workflow_insights(
    repo: Repo,
    ticket_type: str | None = Query(None, description="Feature, Bug, Story, etc."),
    status: str | None = Query(None, description="Ticket status filter"),
    min_llm_cost: float | None = Query(None, description="Minimum LLM cost filter"),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """All tickets from silver___ticket_full_view with optional filters.

    Sorted by LLM cost descending so the most expensive tickets appear first.
    """
    stmt = (
        select(literal_column("*"))
        .select_from(_ticket_full_view)
        .order_by(column("llm_total_cost").desc())
        .limit(limit)
    )
    if ticket_type:
        stmt = stmt.where(column("ticket_type") == ticket_type)
    if status:
        stmt = stmt.where(column("ticket_status") == status)
    if min_llm_cost is not None:
        stmt = stmt.where(column("llm_total_cost") >= min_llm_cost)
    return _rows(repo, stmt)


@app.get("/insights/stage-performance", tags=["silver"])
def get_stage_performance(repo: Repo) -> list[dict]:
    """Per-stage LLM cost, avg latency, and approval rate.

    Queries silver___stage_performance which joins llm_traces with
    human_interactions by workflow stage.
    """
    stmt = (
        select(literal_column("*"))
        .select_from(_stage_performance)
        .order_by(column("trace_count").desc())
    )
    return _rows(repo, stmt)


@app.get("/insights/prs", tags=["silver"])
def get_pr_insights(
    repo: Repo,
    merged_only: bool = Query(False, description="Only return merged PRs"),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """PR metrics with correlated LLM cost from silver___pr_with_llm_cost."""
    stmt = (
        select(literal_column("*"))
        .select_from(_pr_with_llm_cost)
        .order_by(column("created_at").desc())
        .limit(limit)
    )
    if merged_only:
        stmt = stmt.where(column("merged") == True)  # noqa: E712
    return _rows(repo, stmt)


@app.get("/insights/cost-by-model", tags=["silver"])
def get_cost_by_model(repo: Repo, days: int = Query(30, ge=1, le=90)) -> list[dict]:
    """LLM cost and trace volume grouped by trace name over the last N days."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(
            column("name").label("trace_name"),
            func.count().label("trace_count"),
            func.round(func.sum(column("total_cost")), 4).label("total_cost"),
            func.round(func.avg(column("latency_ms")) / 1000, 2).label("avg_latency_sec"),
        )
        .select_from(_llm_traces)
        .where(column("timestamp") >= cutoff)
        .group_by(column("name"))
        .order_by(literal_column("total_cost").desc())
    )
    return _rows(repo, stmt)
