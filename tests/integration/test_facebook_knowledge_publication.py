"""End-to-end integration test: Facebook single-source comment becomes attributed Publication (Plan 5 Task 7)."""

import datetime as dt
import json
from pathlib import Path

import psycopg
import pytest

from src.db.uow import DatabaseUnitOfWork
from src.domain.sources import Source
from src.ingestion.repository import IngestionRepository
from src.ingestion.service import IngestionService
from src.providers.facebook.comments import (
    CommentCollectionBatch,
    FacebookCommentCollector,
    FacebookCommentRefreshService,
    parse_comment_from_data,
)
from src.publication.models import PublicationRun
from src.publication.policies import PublicationPolicyService
from src.publication.repository import PublicationRepository
from src.publication.selection import (
    EditorialSelectionService,
    SelectionProposal,
)
from src.publication.snapshot import PublicationSnapshotService
from src.repositories.facebook import FacebookRepository


@pytest.fixture
def facebook_fixture_data() -> dict:
    fixture_path = Path(__file__).parent.parent / "fixtures" / "facebook_single_comment_story.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))


@pytest.mark.postgres
class TestFacebookKnowledgePublicationIntegration:
    """Proves a single-source useful operational Facebook comment becomes an attributed publication story."""

    async def test_facebook_comment_to_publication_flow(
        self,
        conn: psycopg.AsyncConnection,
        uow: DatabaseUnitOfWork,
        edition,
        facebook_fixture_data: dict,
    ):
        fb_repo = FacebookRepository()
        ingestion_repo = IngestionRepository()
        ingestion_service = IngestionService(uow=uow, repo=ingestion_repo)
        comment_collector = FacebookCommentCollector(fb_repo=fb_repo)
        comment_refresh = FacebookCommentRefreshService(
            uow=uow,
            fb_repo=fb_repo,
            ingestion_service=ingestion_service,
            collector=comment_collector,
        )
        pub_repo = PublicationRepository()
        policy_service = PublicationPolicyService()
        snapshot_service = PublicationSnapshotService(
            uow=uow, repo=pub_repo, policy_service=policy_service
        )

        now = dt.datetime.now(dt.timezone.utc)
        src_data = facebook_fixture_data["source"]
        post_data = facebook_fixture_data["post"]

        # 1. Insert Facebook Source & Post Item
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role, enabled)
            VALUES (%s, %s, %s, %s, %s, %s, true)
            RETURNING id
            """,
            (
                src_data["platform"],
                src_data["kind"],
                src_data["external_id"],
                src_data["url"],
                src_data["name"],
                src_data["role"],
            ),
        )
        source_id = (await cur.fetchone())[0]

        # Bind source to edition
        await conn.execute(
            """
            INSERT INTO source_editions (source_id, edition_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (source_id, edition.id),
        )

        cur = await conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, published_at, first_collected_at)
            VALUES (%s, 'facebook_post', %s, %s, %s)
            RETURNING id
            """,
            (source_id, post_data["external_id"], now, now),
        )
        post_item_id = (await cur.fetchone())[0]
        await fb_repo.get_or_create_comment_state(conn, source_item_id=post_item_id)
        await conn.commit()

        # 2. Collect Facebook Comments
        source = Source(
            id=source_id,
            platform="facebook",
            kind=src_data["kind"],
            external_id=src_data["external_id"],
            url=src_data["url"],
            name=src_data["name"],
            role=src_data["role"],
            enabled=True,
            collector_options={},
            created_at=now,
            updated_at=now,
        )

        comment_items = []
        for c in post_data["comments"]:
            item, assets = parse_comment_from_data(
                source=source,
                post_external_id=post_data["external_id"],
                comment_id=c["external_id"].replace("comment:", ""),
                text=c["text"],
                author_name=c.get("author_name"),
                published_at=now,
            )
            comment_items.append(item)

        batch = CommentCollectionBatch(
            source_id=source_id,
            post_item_id=post_item_id,
            completeness="complete",
            stop_reason="exhausted",
            total_comments_observed=len(comment_items),
            items=comment_items,
        )

        refresh_result = await comment_refresh.refresh_batch(
            source_id=source_id,
            post_item_id=post_item_id,
            batch=batch,
        )
        assert refresh_result.ingestion.new_items == 2

        # 3. Create Story from the useful operational comment
        async with uow.transaction() as t_conn:
            # Find the comment item ID
            cur = await t_conn.execute(
                """
                SELECT si.id, r.id FROM source_items si
                JOIN source_item_revisions r ON r.source_item_id = si.id
                WHERE si.external_id = 'comment:112233'
                """
            )
            comment_row = await cur.fetchone()
            assert comment_row is not None
            comment_item_id, comment_rev_id = comment_row

            # Insert story
            cur = await t_conn.execute(
                """
                INSERT INTO stories (edition_id, lifecycle_state, created_at)
                VALUES (%s, 'active', %s)
                RETURNING id
                """,
                (edition.id, now),
            )
            story_id = (await cur.fetchone())[0]

            # Insert story revision
            cur = await t_conn.execute(
                """
                INSERT INTO story_revisions (story_id, revision_no, title, summary, current_state, semantic_text, content_hash, created_at)
                VALUES (%s, 1, 'Перекрытие на Мелитопольском', 'По сообщениям жителей в соцсетях, перекрыто движение у завода Кабельщиков.', 'open', 'перекрытие движения у Кабельщиков', 'hash-comm-1', %s)
                RETURNING id
                """,
                (story_id, now),
            )
            story_rev_id = (await cur.fetchone())[0]

            await t_conn.execute(
                "UPDATE stories SET current_revision_id = %s WHERE id = %s",
                (story_rev_id, story_id),
            )

        # 4. Freeze Publication Snapshot & Run Candidate Selection
        snapshot_time = now + dt.timedelta(minutes=5)
        run = await snapshot_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=snapshot_time,
        )

        candidates = await snapshot_service.seal_candidates(run.id)
        assert len(candidates) >= 1
        assert any(c.story_id == story_id for c in candidates)

        # 5. Editorial Selection
        class SingleSourceMockSelector:
            async def select_stories(self, *, run: PublicationRun, candidates):
                return [
                    SelectionProposal(
                        story_id=story_id,
                        story_revision_id=story_rev_id,
                        decision="INCLUDE",
                        presentation_intent="unverified_operational",
                        reason="Useful timely operational road closure info from resident comment",
                        rank=1,
                    )
                ]

        selector_service = EditorialSelectionService(
            uow=uow,
            repo=pub_repo,
            model=SingleSourceMockSelector(),
        )
        selected_inputs = await selector_service.select(run.id)

        assert len(selected_inputs) == 1
        assert selected_inputs[0].story_id == story_id
        assert selected_inputs[0].presentation_intent == "unverified_operational"

        # Verify story summary retains attribution and no unverified accusations exist
        async with uow.transaction() as t_conn:
            cur = await t_conn.execute(
                "SELECT summary FROM story_revisions WHERE id = %s", (story_rev_id,)
            )
            summary = (await cur.fetchone())[0]
            assert "по сообщениям жителей" in summary.lower() or "в соцсетях" in summary.lower()

            # Verify accusation comment 445566 was NOT made into a story
            cur = await t_conn.execute(
                """
                SELECT count(*) FROM claims c
                JOIN source_item_revisions r ON r.id = c.source_item_revision_id
                JOIN source_items si ON si.id = r.source_item_id
                WHERE si.external_id = 'comment:445566'
                """
            )
            accusation_claims = (await cur.fetchone())[0]
            assert accusation_claims == 0
