"""Tests for provider-neutral enrichment planner and dispatcher (Plan 5 Task 5)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest

from src.ingestion.enrichment import (
    EnrichmentPlanner,
    EnrichmentRequest,
    get_enrichment_dispatcher,
    plan_facebook_comments,
)


def _make_relevance_decision(status: str = "relevant") -> SimpleNamespace:
    return SimpleNamespace(
        id=10,
        source_item_revision_id=100,
        edition_id=1,
        status=status,
    )


def _make_revision(
    platform: str = "facebook", kind: str = "facebook_post", source_item_id: int = 50
) -> SimpleNamespace:
    return SimpleNamespace(
        id=100,
        source_item_id=source_item_id,
        platform=platform,
        kind=kind,
        payload={"platform": platform},
    )


class TestEnrichmentPlanner:
    """Unit tests for enrichment planning rules."""

    def test_irrelevant_decision_produces_no_enrichment_request(self):
        decision = _make_relevance_decision(status="irrelevant")
        revision = _make_revision(platform="facebook", kind="facebook_post")

        planner = EnrichmentPlanner()
        planner.register_rule(plan_facebook_comments)

        with patch("src.providers.facebook.runtime_policy.is_facebook_enabled", return_value=True):
            reqs = planner.requests_for(decision, revision)
            assert reqs == []

    def test_relevant_facebook_post_produces_comments_enrichment_request(self):
        decision = _make_relevance_decision(status="relevant")
        revision = _make_revision(platform="facebook", kind="facebook_post", source_item_id=77)

        planner = EnrichmentPlanner()
        planner.register_rule(plan_facebook_comments)

        with patch("src.providers.facebook.runtime_policy.is_facebook_enabled", return_value=True):
            reqs = planner.requests_for(decision, revision)
            assert len(reqs) == 1
            req = reqs[0]
            assert req.kind == "facebook_comments"
            assert req.source_item_revision_id == 100
            assert req.mode == "incremental"
            assert req.metadata["post_item_id"] == 77

    def test_disabled_facebook_produces_no_enrichment_request(self):
        decision = _make_relevance_decision(status="relevant")
        revision = _make_revision(platform="facebook", kind="facebook_post", source_item_id=77)

        planner = EnrichmentPlanner()
        planner.register_rule(plan_facebook_comments)

        with patch("src.providers.facebook.runtime_policy.is_facebook_enabled", return_value=False):
            reqs = planner.requests_for(decision, revision)
            assert reqs == []

    def test_relevant_telegram_post_produces_no_facebook_enrichment_request(self):
        decision = _make_relevance_decision(status="relevant")
        revision = _make_revision(platform="telegram", kind="channel_message")

        planner = EnrichmentPlanner()
        planner.register_rule(plan_facebook_comments)

        with patch("src.providers.facebook.runtime_policy.is_facebook_enabled", return_value=True):
            reqs = planner.requests_for(decision, revision)
            assert reqs == []


@pytest.mark.postgres
class TestEnrichmentDispatcherIntegration:
    """Integration test for enrichment task deferral with locks."""

    async def test_defer_facebook_comments_applies_locks(
        self, conn: psycopg.AsyncConnection, jobs_import_env: str
    ):
        dispatcher = get_enrichment_dispatcher()
        request = EnrichmentRequest(
            kind="facebook_comments",
            source_item_revision_id=100,
            mode="incremental",
            metadata={"post_item_id": 55, "auth_profile": "primary"},
        )

        with patch("src.providers.facebook.runtime_policy.is_facebook_enabled", return_value=True):
            with patch("src.jobs.facebook.refresh_facebook_comments.configure") as mock_conf:
                mock_task = MagicMock()
                mock_task.defer_async = AsyncMock(return_value=12345)
                mock_conf.return_value = mock_task

                job_id = await dispatcher.defer(conn, request, priority=5)

                assert job_id == 12345
                mock_conf.assert_called_once_with(
                    connection=conn,
                    lock="facebook-auth-profile:primary",
                    queueing_lock="facebook-comments:55:incremental",
                    priority=5,
                )
                mock_task.defer_async.assert_awaited_once_with(
                    source_item_revision_id=100,
                    post_item_id=55,
                    mode="incremental",
                )

    async def test_defer_facebook_comments_bypasses_when_disabled(
        self, conn: psycopg.AsyncConnection, jobs_import_env: str
    ):
        dispatcher = get_enrichment_dispatcher()
        request = EnrichmentRequest(
            kind="facebook_comments",
            source_item_revision_id=100,
            mode="incremental",
            metadata={"post_item_id": 55, "auth_profile": "primary"},
        )

        with patch("src.providers.facebook.runtime_policy.is_facebook_enabled", return_value=False):
            with patch("src.jobs.facebook.refresh_facebook_comments.configure") as mock_conf:
                job_id = await dispatcher.defer(conn, request, priority=5)
                assert job_id is None
                mock_conf.assert_not_called()
