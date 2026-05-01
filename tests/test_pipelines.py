"""Tests for pure pipeline helper functions — no network, no ClickHouse."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.observability.pipelines.github_pipeline import _extract_ticket_key
from forge.observability.pipelines.jira_pipeline import _classify_interaction


def _make_settings(**overrides):
    """Minimal settings mock with only prometheus enabled (always-on source)."""
    s = MagicMock()
    s.prometheus_enabled = True
    s.langfuse_enabled = False
    s.github_enabled = False
    s.jira_enabled = False
    s.prometheus_interval_seconds = 60
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class TestSkipDbtFlag:
    @pytest.fixture(autouse=True)
    def _patch_worker_deps(self):
        """Patch all I/O in worker so no ClickHouse or network is needed."""
        with (
            patch(
                "forge.observability.pipelines.worker.get_settings",
                return_value=_make_settings(),
            ),
            patch(
                "forge.observability.pipelines.worker._load_pipeline",
                new_callable=AsyncMock,
            ) as mock_load,
            patch("forge.observability.pipelines.worker._load_dbt") as mock_dbt,
            patch(
                "forge.observability.pipelines.worker._get_available_sources",
                return_value=[],
            ),
        ):
            self.mock_load = mock_load
            self.mock_dbt = mock_dbt
            yield

    async def test_once_skip_dbt_does_not_call_dbt(self):
        from forge.observability.pipelines.worker import _run_pipelines

        await _run_pipelines(once=True, skip_dbt=True)

        self.mock_load.assert_called_once()
        self.mock_dbt.assert_not_called()

    async def test_once_without_skip_dbt_calls_dbt(self):
        from forge.observability.pipelines.worker import _run_pipelines

        await _run_pipelines(once=True, skip_dbt=False)

        self.mock_load.assert_called_once()
        self.mock_dbt.assert_called_once()

    async def test_continuous_skip_dbt_does_not_call_dbt(self):
        from forge.observability.pipelines.worker import _run_pipelines

        task = asyncio.create_task(_run_pipelines(once=False, skip_dbt=True))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        self.mock_dbt.assert_not_called()


class TestExtractTicketKey:
    def test_finds_key_in_title(self):
        assert _extract_ticket_key("FOR-123 implement login") == "FOR-123"

    def test_finds_key_in_branch_name(self):
        assert _extract_ticket_key("feature/AB-42-add-auth") == "AB-42"

    def test_no_key_returns_empty(self):
        assert _extract_ticket_key("chore: update dependencies") == ""

    def test_empty_string(self):
        assert _extract_ticket_key("") == ""

    def test_key_must_have_digits(self):
        assert _extract_ticket_key("NOPE fix something") == ""

    def test_returns_first_match(self):
        assert _extract_ticket_key("FOR-1 fixes AB-2 issue") == "FOR-1"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("PROJ-999: big feature", "PROJ-999"),
            ("[TEAM-7] hotfix", "TEAM-7"),
            ("ref: XY-0", "XY-0"),
        ],
    )
    def test_various_formats(self, text, expected):
        assert _extract_ticket_key(text) == expected


class TestClassifyInteraction:
    # ── changelog-based classification ────────────────────────────────────

    def test_approval_label_change(self):
        items = [{"field": "labels", "toString": "forge:prd-approved added"}]
        assert _classify_interaction(items, "") == "approval"

    def test_rejection_via_blocked_label(self):
        items = [{"field": "labels", "toString": "forge:blocked"}]
        assert _classify_interaction(items, "") == "rejection"

    def test_unrelated_field_is_ignored(self):
        items = [{"field": "status", "toString": "In Progress"}]
        # No comment body either → returns None
        assert _classify_interaction(items, "") is None

    # ── comment-based classification ──────────────────────────────────────

    def test_lgtm_comment_is_approval(self):
        assert _classify_interaction([], "LGTM!") == "approval"

    def test_approve_word_is_approval(self):
        assert _classify_interaction([], "looks good, approved") == "approval"

    def test_question_mark_is_question(self):
        assert _classify_interaction([], "What does this do?") == "question"

    def test_clarify_keyword_is_question(self):
        assert _classify_interaction([], "can you clarify the intent here") == "question"

    def test_plain_comment_defaults_to_rejection(self):
        assert _classify_interaction([], "this needs more work") == "rejection"

    def test_empty_comment_no_items_returns_none(self):
        assert _classify_interaction([], "") is None

    def test_changelog_takes_precedence_over_comment(self):
        items = [{"field": "labels", "toString": "forge:prd-approved"}]
        result = _classify_interaction(items, "this needs more work")
        assert result == "approval"
