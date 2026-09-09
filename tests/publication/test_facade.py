"""Tests for publication facade request and preview orchestration (Plan 4 Task 8 & Unification)."""

import datetime as dt

import psycopg
import pytest

from src.config_loader import Config
from src.publication.facade import build_publication_preview, request_publication

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


@pytest.mark.postgres
class TestPublicationFacade:
    """Tests for request_publication and build_publication_preview."""

    @pytest.fixture
    def pub_config(self, sample_config: Config, database_config) -> Config:
        import dataclasses

        settings = dataclasses.replace(sample_config.settings, persistent_ingestion=True)
        return dataclasses.replace(sample_config, database=database_config, settings=settings)

    @pytest.fixture(autouse=True)
    def _install_runtime(self, uow, pool, production_jobs_app):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )

    async def test_build_publication_preview_generates_output_without_deferring_delivery(
        self, conn: psycopg.AsyncConnection, pool, edition, pub_config: Config
    ):
        """Preview runs pipeline in-process, saves publication with preview=true, but defers 0 delivery jobs."""
        from tests.publication.conftest import seed_claim_for_story

        # Seed story with claim
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Сюжет для превью дайджеста', 'h-prev', %s) RETURNING id
            """,
            (story_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )
        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

        # Clear any procrastinate jobs before preview
        await conn.execute("DELETE FROM procrastinate_jobs")

        preview = await build_publication_preview(
            publication_type="digest_grouped",
            edition_slug="berdyansk",
            snapshot_at=_NOW,
            config=pub_config,
        )

        assert preview.run_id > 0
        assert preview.publication_id > 0
        assert len(preview.body) > 0
        assert preview.publication_type == "digest_grouped"

        # Check no procrastinate jobs were queued
        cur = await conn.execute("SELECT count(*) FROM procrastinate_jobs")
        job_count = (await cur.fetchone())[0]
        assert job_count == 0

        # Check publication has preview metadata
        cur = await conn.execute(
            "SELECT metadata FROM publications WHERE id = %s",
            (preview.publication_id,),
        )
        pub_row = await cur.fetchone()
        assert pub_row[0].get("preview") is True

    async def test_build_publication_preview_lookback_override(
        self, conn: psycopg.AsyncConnection, pool, edition, pub_config: Config
    ):
        """Passing lookback_hours creates eligibility policy with that exact lookback."""
        from tests.publication.conftest import seed_claim_for_story

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Сюжет для 6-часового превью', 'h-6h', %s) RETURNING id
            """,
            (story_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )
        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

        preview = await build_publication_preview(
            publication_type="digest_grouped",
            edition_slug="berdyansk",
            snapshot_at=_NOW,
            lookback_hours=6,
            config=pub_config,
        )

        # Verify the run's eligibility policy version has lookback_hours = 6
        cur = await conn.execute(
            """
            SELECT epv.config->'lookback_hours'
            FROM publication_runs pr
            JOIN eligibility_policy_versions epv ON epv.id = pr.eligibility_policy_id
            WHERE pr.id = %s
            """,
            (preview.run_id,),
        )
        row = await cur.fetchone()
        assert int(row[0]) == 6

    async def test_request_publication_creates_intent_before_preparation(
        self, conn: psycopg.AsyncConnection, pool, edition, pub_config: Config
    ):
        """Manual requests create a durable intent; no run exists without ready sources."""
        await conn.execute("DELETE FROM procrastinate_jobs")

        res = await request_publication(
            publication_type="digest_grouped",
            edition_slug="berdyansk",
            snapshot_at=_NOW,
            config=pub_config,
        )

        assert res.intent_id > 0
        assert res.readiness_status == "failed"
        cur = await conn.execute(
            "SELECT status FROM publication_refresh_runs WHERE id = %s", (res.intent_id,)
        )
        assert (await cur.fetchone())[0] == "failed"
        cur = await conn.execute("SELECT count(*) FROM publication_runs")
        assert (await cur.fetchone())[0] == 0

    async def test_article_preview_propagates_terminal_rejection(
        self, conn: psycopg.AsyncConnection, pool, edition, pub_config: Config, monkeypatch
    ):
        """Preview generation propagates ArticlePublicationRejected when article generation fails."""
        from src.publication.errors import ArticlePublicationRejected

        async def reject(*args, **kwargs):
            assert kwargs["defer_delivery"] is False
            assert kwargs["publication_metadata"] == {
                "preview": True,
                "preview_mode": "no_delivery",
            }
            raise ArticlePublicationRejected(
                reason="validation_failed",
                message="preview article rejected",
            )

        monkeypatch.setattr(
            "src.publication.generation.PublicationGenerationService.generate",
            reject,
        )

        with pytest.raises(ArticlePublicationRejected) as caught:
            await build_publication_preview(
                "article",
                edition_slug="berdyansk",
                config=pub_config,
            )
        assert caught.value.reason == "validation_failed"
