"""Tests for the FastAPI application — repository is mocked."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from forge.observability.api.app import app


@pytest.fixture(autouse=True)
def mock_repository():
    """Replace get_repository with a fresh mock for every test."""
    mock = MagicMock()
    with patch("forge.observability.api.app.get_repository", return_value=mock):
        yield mock


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ── /health ────────────────────────────────────────────────────────────────


class TestHealth:
    def test_ok(self, client, mock_repository):
        mock_repository.query_one.return_value = {"1": 1}
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "store": "sqlalchemy"}

    def test_db_down_returns_503(self, client, mock_repository):
        mock_repository.query_one.side_effect = RuntimeError("connection refused")
        resp = client.get("/health")
        assert resp.status_code == 503


# ── /traces ────────────────────────────────────────────────────────────────


class TestTraces:
    def test_returns_rows(self, client, mock_repository):
        mock_repository.query.return_value = [{"trace_id": "t1", "name": "plan"}]
        resp = client.get("/traces")
        assert resp.status_code == 200
        assert resp.json()[0]["trace_id"] == "t1"

    def test_with_limit(self, client, mock_repository):
        mock_repository.query.return_value = []
        resp = client.get("/traces?limit=10")
        assert resp.status_code == 200
        mock_repository.query.assert_called_once()

    def test_with_ticket_key_filter(self, client, mock_repository):
        mock_repository.query.return_value = []
        resp = client.get("/traces?ticket_key=FOR-99")
        assert resp.status_code == 200
        mock_repository.query.assert_called_once()

    def test_db_error_returns_503(self, client, mock_repository):
        mock_repository.query.side_effect = RuntimeError("db error")
        resp = client.get("/traces")
        assert resp.status_code == 503


# ── /tickets/{key}/summary ─────────────────────────────────────────────────


class TestTicketSummary:
    def test_returns_row(self, client, mock_repository):
        mock_repository.query_one.return_value = {"ticket_key": "FOR-1", "llm_total_cost": 0.5}
        resp = client.get("/tickets/FOR-1/summary")
        assert resp.status_code == 200
        assert resp.json()["ticket_key"] == "FOR-1"

    def test_not_found_returns_404(self, client, mock_repository):
        mock_repository.query_one.return_value = None
        resp = client.get("/tickets/DOES-NOT-EXIST/summary")
        assert resp.status_code == 404


# ── /insights/workflows ────────────────────────────────────────────────────


class TestWorkflowInsights:
    def test_empty_result(self, client, mock_repository):
        mock_repository.query.return_value = []
        resp = client.get("/insights/workflows")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_with_filters(self, client, mock_repository):
        mock_repository.query.return_value = []
        resp = client.get("/insights/workflows?ticket_type=Bug&status=Done&min_llm_cost=0.1")
        assert resp.status_code == 200
        mock_repository.query.assert_called_once()


# ── /insights/prs ──────────────────────────────────────────────────────────


class TestPrInsights:
    def test_all_prs(self, client, mock_repository):
        mock_repository.query.return_value = [{"pr_number": 42}]
        resp = client.get("/insights/prs")
        assert resp.status_code == 200

    def test_merged_only(self, client, mock_repository):
        mock_repository.query.return_value = []
        resp = client.get("/insights/prs?merged_only=true")
        assert resp.status_code == 200
        mock_repository.query.assert_called_once()


# ── /ci-checks ─────────────────────────────────────────────────────────────


class TestCiChecks:
    def test_no_filters(self, client, mock_repository):
        mock_repository.query.return_value = []
        resp = client.get("/ci-checks")
        assert resp.status_code == 200

    def test_with_repo_filter(self, client, mock_repository):
        mock_repository.query.return_value = []
        resp = client.get("/ci-checks?repo=org/myrepo")
        assert resp.status_code == 200
        mock_repository.query.assert_called_once()
