"""Tests for Publication generation attempts and publication constraints (Plan 4 Task 1)."""

import datetime as dt

import psycopg
import pytest

from src.publication.repository import (
    PublicationPolicyRepository,
    PublicationRepository,
)

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


async def _seed_policies(conn: psycopg.AsyncConnection, edition_id: int) -> tuple[int, int, int]:
    policy_repo = PublicationPolicyRepository()
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn, edition_id=edition_id, config_hash="elig-hash-1", prompt_version="elig-v1"
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition_id, config_hash="sel-hash-1", prompt_version="sel-v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition_id, config_hash="wri-hash-1", prompt_version="wri-v1"
    )
    return (elig.id, sel.id, wri.id)


@pytest.mark.postgres
class TestPublicationGenerationConstraints:
    """Tests DB constraints for generation attempts and publications."""

    async def test_winning_attempt_belongs_to_same_publication_run(
        self, conn: psycopg.AsyncConnection, edition
    ):
        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        run1 = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-key-run1",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        run2 = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-key-run2",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )

        attempt_in_run2 = await repo.insert_generation_attempt(
            conn,
            run_id=run2.id,
            attempt_no=1,
            kind="writer",
            provider="mock",
        )

        # Attempting to create Publication in run1 with an attempt from run2 must fail FK
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await repo.create_publication(
                conn,
                run_id=run1.id,
                winning_attempt_id=attempt_in_run2.id,
                publication_type="article",
                title="Заголовок",
                lead="Лид",
                body="Текст статьи",
            )

    async def test_publication_body_cannot_be_null(self, conn: psycopg.AsyncConnection, edition):
        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        run = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-key-not-null-body",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        attempt = await repo.insert_generation_attempt(
            conn,
            run_id=run.id,
            attempt_no=1,
            kind="writer",
            provider="mock",
        )

        with pytest.raises(psycopg.errors.NotNullViolation):
            await conn.execute(
                """
                INSERT INTO publications (
                    publication_run_id, winning_generation_attempt_id,
                    publication_type, title, body
                ) VALUES (%s, %s, 'article', 'Заголовок', NULL)
                """,
                (run.id, attempt.id),
            )
