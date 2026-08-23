"""Story aggregate persistence tests (Plan 3 Task 6).

Covers spec §18 shapes: immutable story revisions with the composite
DEFERRABLE FK proving the current revision belongs to the same story,
meaningful-change-only revisions, explicit lifecycle state events, and the
exclusive/idempotent claim membership (`story_claims`, spec §19).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from types import SimpleNamespace

import psycopg
import pytest

from src.domain.claims import NewClaim
from src.domain.stories import (
    NewStoryRevision,
    Story,
    StoryRelation,
    StoryStateEvent,
    StoryWithRevision,
)
from src.repositories.claims import (
    ClaimExtractionPolicyRepository,
    ClaimExtractionRunRepository,
    ClaimRepository,
)
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
    RelevancePolicyVersionRepository,
)
from src.repositories.stories import StoryRepository

STORY_REPO = StoryRepository()

_RELEVANCE_POLICY_REPO = RelevancePolicyVersionRepository()
_DECISION_REPO = EditionRelevanceDecisionRepository()
_EXTRACTION_POLICY_REPO = ClaimExtractionPolicyRepository()
_RUN_REPO = ClaimExtractionRunRepository()
_CLAIM_REPO = ClaimRepository()

_T0 = dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.timezone.utc)
_T1 = dt.datetime(2026, 8, 22, 12, 30, tzinfo=dt.timezone.utc)

_COUNTER = {"n": 0}


def _next_n() -> int:
    _COUNTER["n"] += 1
    return _COUNTER["n"]


async def _make_claim(
    conn: psycopg.AsyncConnection,
    edition_id: int,
    source_item_revision_id: int,
    assertion: str | None = None,
):
    """Spec §15 chain: relevance decision -> succeeded run -> one claim."""
    n = _next_n()
    relevance_policy = await _RELEVANCE_POLICY_REPO.insert(
        conn,
        edition_id=edition_id,
        version=n,
        config_hash=f"relevance-cfg-{n}",
        prompt_version="relevance-prompt-1",
    )
    decision = await _DECISION_REPO.insert_root(
        conn,
        source_item_revision_id=source_item_revision_id,
        edition_id=edition_id,
        relevance_policy_id=relevance_policy.id,
        status="relevant",
        confidence=None,
        reason="test setup",
    )
    extraction_policy = await _EXTRACTION_POLICY_REPO.insert(
        conn,
        edition_id=edition_id,
        version=n,
        config_hash=f"extraction-cfg-{n}",
        prompt_version="extraction-prompt-1",
    )
    run, _created = await _RUN_REPO.get_or_create_run(
        conn,
        source_item_revision_id=source_item_revision_id,
        edition_id=edition_id,
        extraction_policy_id=extraction_policy.id,
        relevance_decision_id=decision.id,
    )
    assert await _RUN_REPO.mark_succeeded(conn, run.id, completed_at=_T0)
    assertion_text = assertion or f"Утверждение номер {n}: вода пришла на улицу Приморскую."
    claims = await _CLAIM_REPO.insert_claims(
        conn,
        run=run,
        claims=[
            NewClaim(
                assertion_text=assertion_text,
                normalized_assertion=assertion_text.lower(),
            )
        ],
    )
    return claims[0]


def _new_revision(semantic_text: str, *, state: str = "developing") -> NewStoryRevision:
    return NewStoryRevision(
        current_state=state,
        semantic_text=semantic_text,
        content_hash=hashlib.sha256(semantic_text.encode("utf-8")).hexdigest(),
        created_at=_T1,
    )


async def _create_story_with_claim(
    conn: psycopg.AsyncConnection, edition_id: int, source_item_revision_id: int
) -> SimpleNamespace:
    n = _next_n()
    claim = await _make_claim(conn, edition_id, source_item_revision_id)
    result = await STORY_REPO.create_story_with_revision(
        conn,
        edition_id=edition_id,
        claim_id=claim.id,
        revision=_new_revision(f"Разлив топлива у причала, сообщение {n}."),
    )
    return SimpleNamespace(story_id=result.story_id, revision=result.revision, claim=claim)


class TestStoryRepositoryCreation:
    async def test_story_repository_create_returns_active_story_with_first_revision_and_claim(
        self, conn, edition, revision_factory
    ):
        source_revision = await revision_factory()
        claim = await _make_claim(conn, edition.id, source_revision.id)

        result = await STORY_REPO.create_story_with_revision(
            conn,
            edition_id=edition.id,
            claim_id=claim.id,
            revision=_new_revision(
                "На АЗС возле почты сливают топливо в бочки.", state="developing"
            ),
        )

        assert isinstance(result, StoryWithRevision)
        assert result.story_id > 0
        assert result.revision.story_id == result.story_id
        assert result.revision.revision_no == 1
        assert result.revision.semantic_text == "На АЗС возле почты сливают топливо в бочки."
        assert result.revision.current_state == "developing"

        story = await STORY_REPO.get(conn, result.story_id)
        assert story is not None
        assert story.edition_id == edition.id
        assert story.lifecycle_state == "active"
        assert story.current_revision_id == result.revision.id

        cursor = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s", (claim.id,)
        )
        membership = await cursor.fetchone()
        assert membership is not None
        assert membership[0] == result.story_id

    async def test_story_repository_get_and_current_revision_helpers(
        self, conn, edition, revision_factory
    ):
        assert await STORY_REPO.get(conn, 10**9) is None
        assert await STORY_REPO.current_revision_id(conn, 10**9) is None

        source_revision = await revision_factory()
        created = await _create_story_with_claim(conn, edition.id, source_revision.id)

        story = await STORY_REPO.get(conn, created.story_id)
        assert isinstance(story, Story)
        assert story.id == created.story_id
        assert story.edition_id == edition.id
        assert isinstance(story.created_at, dt.datetime)
        assert await STORY_REPO.current_revision_id(conn, created.story_id) == created.revision.id


class TestStoryRepositoryCurrentRevisionInvariant:
    async def test_story_repository_cross_story_current_revision_rejected_immediately(
        self, conn, edition, revision_factory
    ):
        first_source = await revision_factory()
        second_source = await revision_factory()
        first = await _create_story_with_claim(conn, edition.id, first_source.id)
        second = await _create_story_with_claim(conn, edition.id, second_source.id)

        # Composite FK stories(id, current_revision_id) ->
        # story_revisions(story_id, id) is INITIALLY IMMEDIATE: pointing one
        # story at another story's revision fails at statement time.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute(
                "UPDATE stories SET current_revision_id = %s WHERE id = %s",
                (first.revision.id, second.story_id),
            )

    async def test_story_repository_deferred_constraint_satisfied_within_transaction(
        self, conn, edition, revision_factory
    ):
        source_revision = await revision_factory()

        # The shell -> first-revision -> pointer ordering must satisfy the
        # composite FK even when checks are deferred to commit time.
        async with conn.transaction():
            await conn.execute("SET CONSTRAINTS ALL DEFERRED")
            created = await _create_story_with_claim(conn, edition.id, source_revision.id)

        story = await STORY_REPO.get(conn, created.story_id)
        assert story is not None
        assert story.lifecycle_state == "active"
        assert story.current_revision_id == created.revision.id

    async def test_story_repository_deferred_violation_surfaces_at_commit(
        self, conn, edition, revision_factory
    ):
        first_source = await revision_factory()
        second_source = await revision_factory()
        first = await _create_story_with_claim(conn, edition.id, first_source.id)
        second = await _create_story_with_claim(conn, edition.id, second_source.id)

        # Deferral moves the check from statement time to commit time; the
        # cross-story pointer is still rejected by the database. The flag
        # proves the UPDATE itself succeeded at statement time, so ONLY a
        # commit-time violation satisfies this test (a no-op SET CONSTRAINTS
        # with an immediate check would raise before the flag is set).
        update_applied = False
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            async with conn.transaction():
                await conn.execute("SET CONSTRAINTS ALL DEFERRED")
                await conn.execute(
                    "UPDATE stories SET current_revision_id = %s WHERE id = %s",
                    (first.revision.id, second.story_id),
                )
                update_applied = True

        assert update_applied is True


class TestStoryRepositoryRevisionsAndLifecycle:
    async def test_story_repository_unchanged_semantics_creates_no_revision(
        self, conn, edition, revision_factory
    ):
        source_revision = await revision_factory()
        created = await _create_story_with_claim(conn, edition.id, source_revision.id)

        unchanged = await STORY_REPO.create_revision_if_semantic_change(
            conn, story_id=created.story_id, semantic_changed=False, revision=None
        )
        assert unchanged is None
        # A change flag without an explicit payload is a service-contract bug;
        # the repository refuses to invent a revision.
        missing_payload = await STORY_REPO.create_revision_if_semantic_change(
            conn, story_id=created.story_id, semantic_changed=True, revision=None
        )
        assert missing_payload is None

        cursor = await conn.execute(
            "SELECT count(*) FROM story_revisions WHERE story_id = %s",
            (created.story_id,),
        )
        assert (await cursor.fetchone())[0] == 1
        assert await STORY_REPO.current_revision_id(conn, created.story_id) == created.revision.id

    async def test_story_repository_semantic_change_creates_new_revision_and_state_event(
        self, conn, edition, revision_factory
    ):
        source_revision = await revision_factory()
        created = await _create_story_with_claim(conn, edition.id, source_revision.id)

        resolved_event = await STORY_REPO.set_state(
            conn,
            story_id=created.story_id,
            state="resolved",
            reason="Работы завершены",
        )
        reopened_event = await STORY_REPO.set_state(
            conn,
            story_id=created.story_id,
            state="reopened",
            evidence={"claim_ids": [created.claim.id]},
        )
        assert isinstance(resolved_event, StoryStateEvent)
        assert resolved_event.type == "resolved"
        assert resolved_event.reason == "Работы завершены"
        assert isinstance(reopened_event, StoryStateEvent)

        changed_revision = await STORY_REPO.create_revision_if_semantic_change(
            conn,
            story_id=created.story_id,
            semantic_changed=True,
            revision=_new_revision(
                "Разлив расширяется: топливо добралось до пляжа.",
                state="developing",
            ),
        )

        assert changed_revision is not None
        assert changed_revision.revision_no == 2
        assert changed_revision.reason is None
        assert await STORY_REPO.current_revision_id(conn, created.story_id) == changed_revision.id

        story = await STORY_REPO.get(conn, created.story_id)
        assert story is not None
        assert story.lifecycle_state == "reopened"  # reopened stays eligible

        cursor = await conn.execute(
            "SELECT type FROM story_state_events WHERE story_id = %s ORDER BY id",
            (created.story_id,),
        )
        event_types = [row[0] for row in await cursor.fetchall()]
        assert event_types == ["resolved", "reopened"]

    async def test_story_repository_attach_claim_is_idempotent(
        self, conn, edition, revision_factory
    ):
        first_source = await revision_factory()
        second_source = await revision_factory()
        created = await _create_story_with_claim(conn, edition.id, first_source.id)

        # Replay of the identical attachment changes nothing.
        await STORY_REPO.attach_claim(
            conn,
            story_id=created.story_id,
            claim_id=created.claim.id,
            attached_at=_T0,
        )
        cursor = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s",
            (created.claim.id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == created.story_id

        # A different claim joins the same story.
        other_claim = await _make_claim(conn, edition.id, second_source.id)
        await STORY_REPO.attach_claim(
            conn, story_id=created.story_id, claim_id=other_claim.id, attached_at=_T1
        )
        cursor = await conn.execute(
            "SELECT count(*) FROM story_claims WHERE story_id = %s",
            (created.story_id,),
        )
        assert (await cursor.fetchone())[0] == 2

        # Spec §19 exclusivity: a claim already owned by one story is never
        # stolen by another; ON CONFLICT DO NOTHING keeps the original row.
        third_source = await revision_factory()
        other_story = await _create_story_with_claim(conn, edition.id, third_source.id)
        await STORY_REPO.attach_claim(
            conn,
            story_id=other_story.story_id,
            claim_id=created.claim.id,
            attached_at=_T1,
        )
        cursor = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s",
            (created.claim.id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == created.story_id


class TestStoryDomainModels:
    async def test_story_relation_from_row_maps_columns(self):
        relation = StoryRelation.from_row((7, 3, 4, "RELATED_TO", _T0))
        assert relation.id == 7
        assert relation.from_story_id == 3
        assert relation.to_story_id == 4
        assert relation.relation_type == "RELATED_TO"
        assert relation.created_at == _T0


# ---------------------------------------------------------------------------
# Plan 3 Task 7A: story matching policy identity + candidate retrieval
# ---------------------------------------------------------------------------

from src.domain.stories import StoryMatchingPolicyVersion  # noqa: E402
from src.processing.story_matching import (  # noqa: E402
    StoryMatchingPolicyService,
    story_matching_config_hash,
)
from src.repositories.embeddings import (  # noqa: E402
    PURPOSE_STORY_DOCUMENT,
    EmbeddingRepository,
)
from src.repositories.story_candidates import (  # noqa: E402
    REASON_LEXICAL,
    REASON_STATE,
    REASON_VECTOR,
    StoryCandidateRetriever,
    StoryMatchingPolicyVersionRepository,
)

_MATCHING_POLICY_REPO = StoryMatchingPolicyVersionRepository()
_EMBED_REPO = EmbeddingRepository()
_RETRIEVER = StoryCandidateRetriever()

_MODEL_A = "test-embedding-a"

_NOW = dt.datetime.now(dt.timezone.utc)
_AGE_DAYS = dt.timedelta(days=1)


async def _seed_story(
    conn: psycopg.AsyncConnection,
    edition_id: int,
    *,
    semantic_text: str,
    title: str | None = None,
    summary: str | None = None,
    lifecycle_state: str = "active",
    created_at: dt.datetime | None = None,
) -> SimpleNamespace:
    """One story with a single revision wired as its current revision.

    ``created_at`` lands on BOTH the story and the revision rows so the
    state-fallback recency proxy (revision creation moment) is testable.
    """
    cursor = await conn.execute(
        "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, %s, %s) RETURNING id",
        (edition_id, lifecycle_state, created_at or _NOW),
    )
    story_id = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, title, summary, current_state,
            semantic_text, content_hash, created_at
        )
        VALUES (%s, 1, %s, %s, 'open', %s, %s, %s)
        RETURNING id
        """,
        (story_id, title, summary, semantic_text, f"hash-{story_id}", created_at or _NOW),
    )
    revision_id = (await cursor.fetchone())[0]
    await conn.execute(
        "UPDATE stories SET current_revision_id = %s WHERE id = %s",
        (revision_id, story_id),
    )
    return SimpleNamespace(story_id=story_id, revision_id=revision_id)


async def _embed_revision(uow, revision_id: int, embedding: list[float], *, tag: str) -> int:
    async with uow.transaction() as db:
        return await _EMBED_REPO.insert_story_revision_embedding(
            db,
            story_revision_id=revision_id,
            embedding=embedding,
            model=_MODEL_A,
            dimensions=2,
            purpose=PURPOSE_STORY_DOCUMENT,
            content_hash=f"h-{tag}",
        )


async def _insert_policy(
    conn: psycopg.AsyncConnection,
    edition_id: int,
    *,
    vector_limit: int = 20,
    lexical_limit: int = 10,
    state_fallback_limit: int = 20,
    total_candidate_limit: int = 40,
    resolved_lookback_days: int = 30,
) -> StoryMatchingPolicyVersion:
    return await _MATCHING_POLICY_REPO.insert(
        conn,
        edition_id=edition_id,
        version=1,
        config_hash="cfg-test",
        prompt_version="v1",
        vector_limit=vector_limit,
        lexical_limit=lexical_limit,
        state_fallback_limit=state_fallback_limit,
        total_candidate_limit=total_candidate_limit,
        resolved_lookback_days=resolved_lookback_days,
        embedding_model=_MODEL_A,
        embedding_dimensions=2,
    )


async def _retrieve(uow, claim, policy):
    async with uow.transaction() as db:
        return await _RETRIEVER.retrieve(db, claim=claim, claim_embedding=[1.0, 0.0], policy=policy)


@pytest.mark.postgres
class TestStoryMatchingPolicyIdentity:
    async def test_ensure_current_creates_version_one_hashing_full_config(self, conn, edition):
        policy = await StoryMatchingPolicyService().ensure_current(
            conn, edition_id=edition.id, embedding_model="m1", embedding_dimensions=1536
        )

        assert isinstance(policy, StoryMatchingPolicyVersion)
        assert policy.version == 1
        assert policy.edition_id == edition.id
        assert policy.prompt_version == "v1"
        assert policy.embedding_model == "m1"
        assert policy.embedding_dimensions == 1536
        assert policy.vector_limit == 20
        assert policy.lexical_limit == 10
        assert policy.state_fallback_limit == 20
        assert policy.total_candidate_limit == 40
        assert policy.resolved_lookback_days == 30
        assert policy.config_hash == story_matching_config_hash(
            embedding_model="m1",
            embedding_dimensions=1536,
            vector_limit=20,
            lexical_limit=10,
            state_fallback_limit=20,
            total_candidate_limit=40,
            resolved_lookback_days=30,
        )

    async def test_ensure_current_resolves_existing_identity_without_new_row(self, conn, edition):
        service = StoryMatchingPolicyService()
        first = await service.ensure_current(
            conn, edition_id=edition.id, embedding_model="m1", embedding_dimensions=2
        )
        again = await service.ensure_current(
            conn, edition_id=edition.id, embedding_model="m1", embedding_dimensions=2
        )
        assert again.id == first.id

    async def test_changed_config_creates_next_version_and_latest_version_wins(self, conn, edition):
        service = StoryMatchingPolicyService()
        v1 = await service.ensure_current(
            conn, edition_id=edition.id, embedding_model="m1", embedding_dimensions=2
        )
        v2 = await service.ensure_current(
            conn, edition_id=edition.id, embedding_model="m2", embedding_dimensions=3
        )

        assert v2.version == v1.version + 1
        resolved_back = await service.ensure_current(
            conn, edition_id=edition.id, embedding_model="m1", embedding_dimensions=2
        )
        assert resolved_back.id == v1.id

    def test_policy_from_row_maps_positionally(self):
        policy = StoryMatchingPolicyVersion.from_row(
            (9, 4, 2, "hash-x", "v1", 5, 6, 7, 8, 14, "model-x", 512, _T0)
        )
        assert (policy.id, policy.edition_id, policy.version) == (9, 4, 2)
        assert (policy.config_hash, policy.prompt_version) == ("hash-x", "v1")
        assert policy.vector_limit == 5
        assert policy.lexical_limit == 6
        assert policy.state_fallback_limit == 7
        assert policy.total_candidate_limit == 8
        assert policy.resolved_lookback_days == 14
        assert (policy.embedding_model, policy.embedding_dimensions) == ("model-x", 512)
        assert policy.created_at == _T0


@pytest.mark.postgres
class TestStoryCandidateRetrieval:
    async def test_hybrid_union_records_per_stream_provenance(
        self, uow, conn, edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        hybrid = await _seed_story(
            conn,
            edition.id,
            semantic_text="Вода пришла на улицу Приморскую в частный сектор.",
            title="Вода на Приморской",
        )
        lexical_only = await _seed_story(
            conn, edition.id, semantic_text="Вода пришла на улицу Приморскую снова."
        )
        policy = await _insert_policy(conn, edition.id, vector_limit=1)
        await _embed_revision(uow, hybrid.revision_id, [0.99, 0.141], tag="hybrid")

        candidates = await _retrieve(uow, claim, policy)

        by_story = {c.story_id: c for c in candidates}
        # Dedup by story: exactly one candidate row per story.
        assert set(by_story) == {hybrid.story_id, lexical_only.story_id}
        # Every active story is also inside the state-fallback pool, so
        # provenance asserts MEMBERSHIP of the distinguishing signals while
        # excluding the streams that cannot have produced the row.
        hybrid_hit = by_story[hybrid.story_id]
        assert {REASON_VECTOR, REASON_LEXICAL} <= hybrid_hit.retrieval_reasons
        assert hybrid_hit.vector_distance == pytest.approx(0.01, abs=1e-2)
        assert hybrid_hit.lexical_score is not None and hybrid_hit.lexical_score > 0
        assert hybrid_hit.story_revision_embedding_id is not None
        lexical_hit = by_story[lexical_only.story_id]
        assert REASON_LEXICAL in lexical_hit.retrieval_reasons
        assert REASON_VECTOR not in lexical_hit.retrieval_reasons
        assert lexical_hit.vector_distance is None
        assert lexical_hit.lexical_score is not None and lexical_hit.lexical_score > 0
        assert lexical_hit.story_revision_embedding_id is None

    async def test_vector_hit_retained_with_zero_lexical_overlap(
        self, uow, conn, edition, second_edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        foreign = await _seed_story(
            conn, second_edition.id, semantic_text="Вода пришла на улицу Приморскую."
        )
        semantic = await _seed_story(
            conn,
            edition.id,
            semantic_text="Коммунальные службы восстановили электроснабжение района.",
        )
        policy = await _insert_policy(conn, edition.id)
        await _embed_revision(uow, semantic.revision_id, [0.99995, 0.01], tag="semantic")

        candidates = await _retrieve(uow, claim, policy)

        # Edition scoping: the identical-text story of another edition is
        # invisible to this edition's retrieval.
        assert foreign.story_id not in {c.story_id for c in candidates}
        assert [c.story_id for c in candidates] == [semantic.story_id]
        hit = candidates[0]
        assert REASON_VECTOR in hit.retrieval_reasons
        # Zero lexical overlap: the lexical stream cannot have produced this.
        assert REASON_LEXICAL not in hit.retrieval_reasons
        assert hit.vector_distance == pytest.approx(0.00005, abs=1e-3)
        assert hit.lexical_score is None

    async def test_lexical_hit_outside_vector_topk_and_state_hit_without_embedding(
        self, uow, conn, edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        vector_filler = await _seed_story(
            conn,
            edition.id,
            semantic_text="Коммунальные службы работают по графику.",
        )
        lexical_beyond_topk = await _seed_story(
            conn, edition.id, semantic_text="Вода пришла на улицу Приморскую."
        )
        state_no_embedding = await _seed_story(
            conn,
            edition.id,
            semantic_text="Дорогу отремонтировали полностью.",
        )
        policy = await _insert_policy(conn, edition.id, vector_limit=1)
        await _embed_revision(uow, vector_filler.revision_id, [1.0, 0.0], tag="filler")
        await _embed_revision(uow, lexical_beyond_topk.revision_id, [0.0, 1.0], tag="beyond")

        candidates = await _retrieve(uow, claim, policy)

        by_story = {c.story_id: c for c in candidates}
        filler = by_story[vector_filler.story_id]
        beyond = by_story[lexical_beyond_topk.story_id]
        state_hit = by_story[state_no_embedding.story_id]
        # Vector top-K (limit=1) keeps only the exact-match filler...
        assert REASON_VECTOR in filler.retrieval_reasons
        assert REASON_LEXICAL not in filler.retrieval_reasons
        # ...yet the orthogonal lexical match still reaches the matcher.
        assert REASON_LEXICAL in beyond.retrieval_reasons
        assert REASON_VECTOR not in beyond.retrieval_reasons
        assert beyond.lexical_score is not None and beyond.lexical_score > 0
        # A current-revision story with NO completed embedding surfaces via
        # the state stream with a NULL embedding id.
        assert state_hit.retrieval_reasons == frozenset({REASON_STATE})
        assert state_hit.story_revision_embedding_id is None
        assert state_hit.vector_distance is None

    async def test_weak_lexical_signal_is_never_thresholded_away(
        self, uow, conn, edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        weak = await _seed_story(conn, edition.id, semantic_text="Во дворе появилась вода.")
        policy = await _insert_policy(conn, edition.id)

        candidates = await _retrieve(uow, claim, policy)

        weak_hits = [c for c in candidates if c.story_id == weak.story_id]
        assert len(weak_hits) == 1
        assert REASON_LEXICAL in weak_hits[0].retrieval_reasons
        assert weak_hits[0].lexical_score is not None and weak_hits[0].lexical_score > 0

    async def test_empty_database_returns_empty_candidate_list(
        self, uow, conn, edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        policy = await _insert_policy(conn, edition.id)

        candidates = await _retrieve(uow, claim, policy)

        assert candidates == []

    async def test_total_candidate_cap_truncates_deterministic_rank(
        self, uow, conn, edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        nearest = await _seed_story(conn, edition.id, semantic_text="История раз.")
        middle = await _seed_story(conn, edition.id, semantic_text="История два.")
        farthest = await _seed_story(conn, edition.id, semantic_text="История три.")
        policy = await _insert_policy(conn, edition.id, total_candidate_limit=2)
        await _embed_revision(uow, nearest.revision_id, [0.999, 0.001], tag="near")
        await _embed_revision(uow, middle.revision_id, [0.98, 0.02], tag="mid")
        await _embed_revision(uow, farthest.revision_id, [0.95, 0.05], tag="far")

        candidates = await _retrieve(uow, claim, policy)

        assert [c.story_id for c in candidates] == [nearest.story_id, middle.story_id]
        assert candidates[0].vector_distance < candidates[1].vector_distance

    async def test_active_stories_never_age_out_but_old_resolved_do(
        self, uow, conn, edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        active_old = await _seed_story(
            conn,
            edition.id,
            semantic_text="Старая активная история без совпадений.",
            created_at=_NOW - 400 * _AGE_DAYS,
        )
        resolved_recent = await _seed_story(
            conn,
            edition.id,
            semantic_text="Недавно закрытая история.",
            lifecycle_state="resolved",
            created_at=_NOW - 10 * _AGE_DAYS,
        )
        resolved_old = await _seed_story(
            conn,
            edition.id,
            semantic_text="Давно закрытая история вне окна.",
            lifecycle_state="resolved",
            created_at=_NOW - 90 * _AGE_DAYS,
        )
        policy = await _insert_policy(conn, edition.id, resolved_lookback_days=30)

        candidates = await _retrieve(uow, claim, policy)

        by_story = {c.story_id: c for c in candidates}
        assert set(by_story) == {active_old.story_id, resolved_recent.story_id}
        assert resolved_old.story_id not in by_story
        assert REASON_STATE in by_story[active_old.story_id].retrieval_reasons
        assert REASON_STATE in by_story[resolved_recent.story_id].retrieval_reasons


# ---------------------------------------------------------------------------
# Plan 3 Task 7B: matcher prompt/output contract + orchestrator + wiring
# ---------------------------------------------------------------------------

from src.domain.stories import StoryRevision as StoryRevisionRow  # noqa: E402
from src.processing.embeddings import EmbeddingService  # noqa: E402
from src.processing.story_matching import (  # noqa: E402
    InvalidMatchResponse,
    MatcherCandidateView,
    MatchProposal,
    StoryMatcher,
    StoryMatchingService,
)
from src.repositories.claims import ClaimRepository  # noqa: E402
from src.repositories.embeddings import PURPOSE_CLAIM_QUERY  # noqa: E402

_CLAIM_REPO_B = ClaimRepository()


async def _fetch_scalar(db, sql: str, params: tuple = ()):  # noqa: ANN001
    cursor = await db.execute(sql, params)
    return (await cursor.fetchone())[0]


_FULL_ASSERTION = (
    "Утверждение целиком: водоснабжение на улице Приморской, дом 14, "
    "отсутствует вторые сутки после прорыва на вводе."
)
_CANDIDATE_SEMANTIC_A = (
    "Полный смысл истории А: прорыв трубопровода на Приморской оставил "
    "частный сектор без воды, бригада на месте."
)
_CANDIDATE_SEMANTIC_B = "Полный смысл истории Б: плановое отключение света в центре."


def _claim_row(claim_id: int = 1, edition_id: int = 1) -> object:
    return SimpleNamespace(
        id=claim_id,
        claim_extraction_run_id=1,
        source_item_revision_id=1,
        edition_id=edition_id,
        assertion_text="raw",
        normalized_assertion=_FULL_ASSERTION,
        event_time_start=None,
        event_time_end=None,
        event_time_precision=None,
        event_time_confidence=None,
        event_time_original_text=None,
        metadata={},
        created_at=_T0,
    )


def _revision_row(revision_id: int, story_id: int, semantic_text: str) -> StoryRevisionRow:
    return StoryRevisionRow(
        id=revision_id,
        story_id=story_id,
        revision_no=1,
        title="Заголовок истории А",
        summary="Краткое содержание истории А.",
        current_state="developing",
        semantic_text=semantic_text,
        content_hash=f"hash-{revision_id}",
        reason=None,
        created_at=_T0,
    )


class _CapturingProvider:
    """AIProvider double that records chat_completion kwargs verbatim."""

    def __init__(self, response: str):
        self.response = response
        self.calls: list[dict] = []

    async def chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


@pytest.mark.postgres
class TestStoryMatcherPromptContract:
    async def test_prompt_carries_full_assertion_and_full_candidate_texts(self):
        provider = _CapturingProvider('{"assignment": "NEW_STORY"}')
        matcher = StoryMatcher(provider=provider, model="test-model")
        claim = _claim_row()
        views = [
            MatcherCandidateView(
                candidate=SimpleNamespace(
                    story_id=11,
                    story_revision_id=111,
                    retrieval_reasons=frozenset({"retrieved_by_vector"}),
                    vector_distance=0.03,
                    lexical_score=None,
                ),
                revision=_revision_row(111, 11, _CANDIDATE_SEMANTIC_A),
            ),
            MatcherCandidateView(
                candidate=SimpleNamespace(
                    story_id=12,
                    story_revision_id=122,
                    retrieval_reasons=frozenset({"retrieved_by_lexical"}),
                    vector_distance=None,
                    lexical_score=0.42,
                ),
                revision=_revision_row(122, 12, _CANDIDATE_SEMANTIC_B),
            ),
        ]

        proposal = await matcher.choose(claim, views)

        assert proposal.assignment == "NEW_STORY"
        assert len(provider.calls) == 1
        user_content = provider.calls[0]["messages"][-1]["content"]
        # The FULL normalized assertion reaches the model verbatim...
        assert _FULL_ASSERTION in user_content
        # ...and every candidate's complete texts too — never an isolated hit.
        assert _CANDIDATE_SEMANTIC_A in user_content
        assert _CANDIDATE_SEMANTIC_B in user_content
        assert "Заголовок истории А" in user_content
        assert "Краткое содержание истории А." in user_content
        assert "developing" in user_content
        # Retrieval provenance rides along as metadata only.
        assert '"vector_distance": 0.03' in user_content
        assert '"lexical_score": 0.42' in user_content
        assert "retrieved_by_vector" in user_content

    def test_from_dict_accepts_enum_only_assignments(self):
        same = MatchProposal.from_dict(
            {
                "assignment": "SAME_STORY",
                "target_story_id": 7,
                "story_update": {"semantic_changed": False},
                "confidence": 0.9,
                "reason": "same pipe burst",
            }
        )
        assert same.assignment == "SAME_STORY"
        assert same.target_story_id == 7
        assert same.story_update is not None and same.story_update.semantic_changed is False

        fresh = MatchProposal.from_dict(
            {
                "assignment": "NEW_STORY",
                "story_update": {
                    "semantic_changed": True,
                    "title": "Новая история",
                    "current_state": "open",
                    "semantic_text": "Целиком новый смысл.",
                },
                "relation_proposals": [{"to_story_id": 5, "relation_type": "CONSEQUENCE_OF"}],
            }
        )
        assert fresh.target_story_id is None
        assert fresh.story_update.semantic_text == "Целиком новый смысл."
        assert fresh.relation_proposals[0].to_story_id == 5
        assert fresh.relation_proposals[0].relation_type == "CONSEQUENCE_OF"

    def test_from_dict_rejects_non_enum_assignment_and_targetless_same_story(self):
        with pytest.raises(InvalidMatchResponse):
            MatchProposal.from_dict({"assignment": "IRRELEVANT"})
        with pytest.raises(InvalidMatchResponse):
            MatchProposal.from_dict({})
        with pytest.raises(InvalidMatchResponse):
            MatchProposal.from_dict({"assignment": "SAME_STORY"})
        with pytest.raises(InvalidMatchResponse):
            MatchProposal.from_dict("not even a dict")


# ---------------------------------------------------------------------------
# Orchestrator: three-boundary flow over the real database
# ---------------------------------------------------------------------------


async def _seed_claim_embedding(
    uow, claim_id: int, *, model: str = _MODEL_A, dimensions: int = 2
) -> int:
    async with uow.transaction() as db:
        embedding_id = await _EMBED_REPO.insert_claim_embedding(
            db,
            claim_id=claim_id,
            embedding=[0.5] * dimensions,
            model=model,
            dimensions=dimensions,
            purpose=PURPOSE_CLAIM_QUERY,
            content_hash=f"h-claim-{claim_id}-{model}-{dimensions}",
        )
    assert embedding_id is not None
    return embedding_id


def _service(uow, matcher) -> StoryMatchingService:
    return StoryMatchingService(uow=uow, matcher=matcher)


class _FixedMatcher:
    """Returns one validated proposal and records what it was shown."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[tuple[object, list]] = []

    async def choose(self, claim, views, *, edition_name=None):
        del edition_name
        self.calls.append((claim, list(views)))
        return MatchProposal.from_dict(self.payload)


async def _runs_status(conn, run_id: int) -> str:
    cursor = await conn.execute("SELECT status FROM story_matching_runs WHERE id = %s", (run_id,))
    row = await cursor.fetchone()
    assert row is not None
    return str(row[0])


async def _deferred_jobs(pool, task_name: str) -> list[dict]:
    async with pool.connection() as observer:
        cursor = await observer.execute(
            "SELECT args, lock FROM procrastinate.procrastinate_jobs "
            "WHERE task_name = %s ORDER BY id",
            (task_name,),
        )
        return [{"args": dict(row[0]), "lock": row[1]} for row in await cursor.fetchall()]


MATCH_TASK = "src.jobs.processing.match_claim"
EMBED_REVISION_TASK = "src.jobs.processing.embed_story_revision"


@pytest.mark.postgres
class TestSameStoryWithRelationProposals:
    async def test_same_story_attaches_claim_and_persists_consequence_of_proposal(
        self, uow, pool, conn, edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)
        target = await _seed_story(
            conn, edition.id, semantic_text="Прорыв на Приморской оставил сектор без воды."
        )
        other = await _seed_story(
            conn, edition.id, semantic_text="Совершенно другая история про транспорт."
        )
        matcher = _FixedMatcher(
            {
                "assignment": "SAME_STORY",
                "target_story_id": target.story_id,
                "story_update": {"semantic_changed": False},
                "relation_proposals": [
                    {"to_story_id": other.story_id, "relation_type": "CONSEQUENCE_OF"}
                ],
                "confidence": 0.87,
                "reason": "тот же прорыв трубы",
            }
        )

        outcome = await _service(uow, matcher).run(claim.id, policy.id, embedding_id)

        assert outcome.revision is None
        assert outcome.story_id == target.story_id
        assert await _runs_status(conn, outcome.run.id) == "succeeded"
        # Claim attached to the matched story.
        cursor = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s", (claim.id,)
        )
        assert (await cursor.fetchone())[0] == target.story_id
        # One immutable decision row per run.
        cursor = await conn.execute(
            """
            SELECT assignment, target_story_id, confidence, reason
            FROM story_match_decisions WHERE run_id = %s
            """,
            (outcome.run.id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "SAME_STORY"
        assert row[1] == target.story_id
        assert float(row[2]) == pytest.approx(0.87)
        assert row[3] == "тот же прорыв трубы"
        # Independent relation proposal persisted separately.
        cursor = await conn.execute(
            """
            SELECT from_story_id, to_story_id, relation_type
            FROM story_relation_proposals WHERE run_id = %s
            """,
            (outcome.run.id,),
        )
        proposal_row = await cursor.fetchone()
        assert proposal_row == (target.story_id, other.story_id, "CONSEQUENCE_OF")
        # Canonical invariant: exactly one successful run for the key.
        cursor = await conn.execute(
            """
            SELECT count(*) FROM story_matching_runs
            WHERE claim_id = %s AND policy_id = %s AND status = 'succeeded'
            """,
            (claim.id, policy.id),
        )
        assert (await cursor.fetchone())[0] == 1


@pytest.mark.postgres
class TestStaleTargetProtection:
    async def test_target_revision_changed_before_apply_marks_run_stale_and_requeues(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)
        target = await _seed_story(
            conn, edition.id, semantic_text="Прорыв на Приморской оставил сектор без воды."
        )

        class _AdvancingMatcher:
            """Moves the target story forward BETWEEN freeze and apply."""

            def __init__(self, story_id: int):
                self._story_id = story_id

            async def choose(self, claim_arg, views, *, edition_name=None):
                del claim_arg, views, edition_name
                async with uow.transaction() as db:
                    cursor = await db.execute(
                        """
                        INSERT INTO story_revisions (
                            story_id, revision_no, current_state, semantic_text, content_hash
                        )
                        VALUES (%s, 2, 'developing', 'Смысл изменился параллельно.', 'hash-x')
                        RETURNING id
                        """,
                        (self._story_id,),
                    )
                    revision_two = (await cursor.fetchone())[0]
                    await db.execute(
                        "UPDATE stories SET current_revision_id = %s WHERE id = %s",
                        (revision_two, self._story_id),
                    )
                return MatchProposal.from_dict(
                    {
                        "assignment": "SAME_STORY",
                        "target_story_id": self._story_id,
                        "story_update": {"semantic_changed": False},
                    }
                )

        outcome = await _service(uow, _AdvancingMatcher(target.story_id)).run(
            claim.id, policy.id, embedding_id
        )

        assert await _runs_status(conn, outcome.run.id) == "stale"
        assert outcome.stale_rerun_deferred is True
        # No attachment happened on the stale read.
        cursor = await conn.execute(
            "SELECT count(*) FROM story_claims WHERE claim_id = %s", (claim.id,)
        )
        assert (await cursor.fetchone())[0] == 0
        # No verdict was persisted for the stale run.
        cursor = await conn.execute(
            "SELECT count(*) FROM story_match_decisions WHERE run_id = %s",
            (outcome.run.id,),
        )
        assert (await cursor.fetchone())[0] == 0
        # A fresh matching task was deferred on the same connection.
        jobs = await _deferred_jobs(pool, MATCH_TASK)
        assert len(jobs) == 1
        args = jobs[0]["args"]
        assert int(args["claim_id"]) == claim.id
        assert int(args["policy_id"]) == policy.id
        assert int(args["claim_embedding_id"]) == embedding_id


@pytest.mark.postgres
class TestNewStoryAtomicApply:
    async def test_new_story_creates_story_revision_claim_proposal_and_embed_atomically(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)
        existing = await _seed_story(
            conn, edition.id, semantic_text="Родственная история про ту же аварию."
        )
        matcher = _FixedMatcher(
            {
                "assignment": "NEW_STORY",
                "story_update": {
                    "semantic_changed": True,
                    "title": "Вода вернулась в сектор",
                    "summary": "Подача восстановлена не полностью.",
                    "current_state": "developing",
                    "semantic_text": "Новая история целиком: вода вернулась частично.",
                },
                "relation_proposals": [
                    {"to_story_id": existing.story_id, "relation_type": "RELATED_TO"}
                ],
            }
        )

        outcome = await _service(uow, matcher).run(claim.id, policy.id, embedding_id)

        assert outcome.revision is not None
        assert outcome.revision.revision_no == 1
        assert outcome.story_id is not None
        story = await STORY_REPO.get(conn, outcome.story_id)
        assert story is not None and story.lifecycle_state == "active"
        revision_one = await STORY_REPO.current_revision_id(conn, outcome.story_id)
        assert revision_one == outcome.revision.id
        cursor = await conn.execute(
            """
            SELECT title, summary, current_state, semantic_text
            FROM story_revisions WHERE id = %s
            """,
            (outcome.revision.id,),
        )
        title, summary, state, semantic = await cursor.fetchone()
        assert (title, summary, state, semantic) == (
            "Вода вернулась в сектор",
            "Подача восстановлена не полностью.",
            "developing",
            "Новая история целиком: вода вернулась частично.",
        )
        # Founding claim attached atomically.
        cursor = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s", (claim.id,)
        )
        assert (await cursor.fetchone())[0] == outcome.story_id
        # Accepted proposal points FROM the freshly created story.
        cursor = await conn.execute(
            """
            SELECT from_story_id, to_story_id, relation_type
            FROM story_relation_proposals WHERE run_id = %s
            """,
            (outcome.run.id,),
        )
        assert (await cursor.fetchone()) == (
            outcome.story_id,
            existing.story_id,
            "RELATED_TO",
        )
        # Revision #1 embedding deferred in the SAME transaction with the
        # policy-owned vector space.
        jobs = await _deferred_jobs(pool, EMBED_REVISION_TASK)
        assert len(jobs) == 1
        assert int(jobs[0]["args"]["story_revision_id"]) == outcome.revision.id
        assert jobs[0]["args"]["model"] == policy.embedding_model
        assert int(jobs[0]["args"]["dimensions"]) == policy.embedding_dimensions
        assert await _runs_status(conn, outcome.run.id) == "succeeded"

    async def test_exploding_embed_defer_rolls_back_the_whole_apply(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app, monkeypatch
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)

        class _ExplodingDefer:
            def configure(self, **_kwargs):
                return self

            async def defer_async(self, **_kwargs):
                raise RuntimeError("embed revision defer exploded")

        monkeypatch.setattr(jobs_processing, "embed_story_revision", _ExplodingDefer())
        baseline_stories = await conn.execute("SELECT count(*) FROM stories")
        baseline_count = (await baseline_stories.fetchone())[0]

        with pytest.raises(RuntimeError, match="embed revision defer exploded"):
            await _service(
                uow,
                _FixedMatcher(
                    {
                        "assignment": "NEW_STORY",
                        "story_update": {
                            "semantic_changed": True,
                            "current_state": "open",
                            "semantic_text": "Атомарность проверяется откатом.",
                        },
                    }
                ),
            ).run(claim.id, policy.id, embedding_id)

        async with pool.connection() as observer:
            stories_now = await _fetch_scalar(observer, "SELECT count(*) FROM stories")
            claims_attached = await _fetch_scalar(observer, "SELECT count(*) FROM story_claims")
            decisions = await _fetch_scalar(observer, "SELECT count(*) FROM story_match_decisions")
            proposals = await _fetch_scalar(
                observer, "SELECT count(*) FROM story_relation_proposals"
            )
            embed_jobs = await _fetch_scalar(
                observer,
                f"SELECT count(*) FROM procrastinate.procrastinate_jobs "
                f"WHERE task_name = '{EMBED_REVISION_TASK}'",
            )
            running_runs = await _fetch_scalar(
                observer, "SELECT count(*) FROM story_matching_runs WHERE status = 'running'"
            )
            succeeded_runs = await _fetch_scalar(
                observer,
                "SELECT count(*) FROM story_matching_runs WHERE status = 'succeeded'",
            )
        assert stories_now == baseline_count
        assert claims_attached == 0
        assert decisions == 0
        assert proposals == 0
        assert embed_jobs == 0
        # The run itself was frozen in boundary one and stays open.
        assert running_runs == 1
        assert succeeded_runs == 0
        del baseline_stories


@pytest.mark.postgres
class TestSameStoryUpdateSemantics:
    async def test_semantic_changed_false_is_attach_only(
        self, uow, pool, conn, edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)
        target = await _seed_story(
            conn, edition.id, semantic_text="Прорыв на Приморской оставил сектор без воды."
        )
        matcher = _FixedMatcher(
            {
                "assignment": "SAME_STORY",
                "target_story_id": target.story_id,
                "story_update": {"semantic_changed": False},
            }
        )

        outcome = await _service(uow, matcher).run(claim.id, policy.id, embedding_id)

        assert outcome.revision is None
        cursor = await conn.execute(
            "SELECT count(*) FROM story_revisions WHERE story_id = %s",
            (target.story_id,),
        )
        assert (await cursor.fetchone())[0] == 1
        assert await STORY_REPO.current_revision_id(conn, target.story_id) == target.revision_id
        jobs = await _deferred_jobs(pool, EMBED_REVISION_TASK)
        assert jobs == []
        cursor = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s", (claim.id,)
        )
        assert (await cursor.fetchone())[0] == target.story_id
        assert await _runs_status(conn, outcome.run.id) == "succeeded"

    async def test_semantic_changed_true_creates_proposed_revision_and_defers_embedding(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)
        target = await _seed_story(
            conn, edition.id, semantic_text="Прорыв на Приморской оставил сектор без воды."
        )
        matcher = _FixedMatcher(
            {
                "assignment": "SAME_STORY",
                "target_story_id": target.story_id,
                "story_update": {
                    "semantic_changed": True,
                    "summary": "Воду дали только в половине домов.",
                    "current_state": "developing",
                    "semantic_text": "Обновлённый смысл: подача возобновилась частично.",
                },
            }
        )

        outcome = await _service(uow, matcher).run(claim.id, policy.id, embedding_id)

        assert outcome.revision is not None
        assert outcome.revision.revision_no == 2
        assert outcome.revision.summary == "Воду дали только в половине домов."
        assert outcome.revision.semantic_text == (
            "Обновлённый смысл: подача возобновилась частично."
        )
        assert await STORY_REPO.current_revision_id(conn, target.story_id) == (outcome.revision.id)
        cursor = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s", (claim.id,)
        )
        assert (await cursor.fetchone())[0] == target.story_id
        jobs = await _deferred_jobs(pool, EMBED_REVISION_TASK)
        assert [int(job["args"]["story_revision_id"]) for job in jobs] == [outcome.revision.id]
        assert {job["args"]["model"] for job in jobs} == {policy.embedding_model}
        assert await _runs_status(conn, outcome.run.id) == "succeeded"


@pytest.mark.postgres
class TestDuplicateExecutionConvergence:
    async def test_second_execution_converges_on_single_succeeded_run(
        self, uow, pool, conn, edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)
        target = await _seed_story(
            conn, edition.id, semantic_text="Прорыв на Приморской оставил сектор без воды."
        )
        service = _service(
            uow,
            _FixedMatcher(
                {
                    "assignment": "SAME_STORY",
                    "target_story_id": target.story_id,
                    "story_update": {"semantic_changed": False},
                }
            ),
        )

        first = await service.run(claim.id, policy.id, embedding_id)
        # A duplicate execution would have created a NEW story had it applied.
        duplicate = await _service(
            uow,
            _FixedMatcher({"assignment": "NEW_STORY"}),
        ).run(claim.id, policy.id, embedding_id)

        assert first.replayed is False
        assert await _runs_status(conn, first.run.id) == "succeeded"
        assert duplicate.replayed is True
        assert duplicate.story_id is None
        cursor = await conn.execute(
            """
            SELECT count(*) FROM story_matching_runs
            WHERE claim_id = %s AND policy_id = %s AND status = 'succeeded'
            """,
            (claim.id, policy.id),
        )
        assert (await cursor.fetchone())[0] == 1
        cursor = await conn.execute("SELECT count(*) FROM stories")
        total_stories = (await cursor.fetchone())[0]
        assert total_stories == 1  # only the seeded target; no phantom story


@pytest.mark.postgres
class TestBackfillStoryMatching:
    async def test_queues_exactly_once_for_compatible_uncovered_embeddings(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure
        from src.jobs.processing import backfill_story_matching

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        covered = await _make_claim(conn, edition.id, (await revision_factory()).id)
        debt_a = await _make_claim(conn, edition.id, (await revision_factory()).id)
        debt_b = await _make_claim(conn, edition.id, (await revision_factory()).id)
        wrong_dims = await _make_claim(conn, edition.id, (await revision_factory()).id)
        covered_embedding = await _seed_claim_embedding(uow, covered.id)
        embedding_a = await _seed_claim_embedding(uow, debt_a.id)
        embedding_b = await _seed_claim_embedding(uow, debt_b.id)
        await _seed_claim_embedding(uow, wrong_dims.id, dimensions=4)
        policy = await _insert_policy(conn, edition.id)
        # A succeeded run covers the first embedding for this exact policy.
        cursor = await conn.execute(
            """
            INSERT INTO story_matching_runs (claim_id, edition_id, policy_id,
                                             claim_embedding_id, completed_at, status)
            VALUES (%s, %s, %s, %s, now(), 'succeeded')
            """,
            (covered.id, edition.id, policy.id, covered_embedding),
        )
        del cursor

        queued = await backfill_story_matching(edition.id, policy.id)

        assert queued == 2
        jobs = await _deferred_jobs(pool, MATCH_TASK)
        assert {
            (int(j["args"]["claim_id"]), int(j["args"]["claim_embedding_id"])) for j in jobs
        } == {
            (debt_a.id, embedding_a),
            (debt_b.id, embedding_b),
        }
        assert {int(j["args"]["policy_id"]) for j in jobs} == {policy.id}
        assert {j["lock"] for j in jobs} == {f"story-matching-edition:{edition.id}"}

        # Simulate the workers having completed every deferred job: each
        # matching execution lands a succeeded run for its exact policy.
        for claim_id, embedding_id in ((debt_a.id, embedding_a), (debt_b.id, embedding_b)):
            await conn.execute(
                """
                INSERT INTO story_matching_runs (claim_id, edition_id, policy_id,
                                                 claim_embedding_id, completed_at, status)
                VALUES (%s, %s, %s, %s, now(), 'succeeded')
                """,
                (claim_id, edition.id, policy.id, embedding_id),
            )

        # Idempotent: a rerun finds no remaining debt.
        assert await backfill_story_matching(edition.id, policy.id) == 0
        assert len(await _deferred_jobs(pool, MATCH_TASK)) == 2


# ---------------------------------------------------------------------------
@pytest.mark.postgres
class TestUnparseableMatcherResponses:
    """Any matcher-response contract violation must degrade safely: guarded
    terminal run failure (never a stuck 'running' row that backfill would
    count as coverage) and an operationally successful task return."""

    async def _run_with_raw_output(self, uow, conn, edition, revision_factory, raw: str):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)
        # The REAL matcher over a provider emitting the raw garbage output —
        # exercises _parse_json_object -> MatchProposal.from_dict end to end.
        matcher = StoryMatcher(provider=_CapturingProvider(raw), model="test-model")
        return await _service(uow, matcher).run(claim.id, policy.id, embedding_id)

    async def _assert_terminal_failure(self, conn, outcome, run_id: int):
        assert outcome.degraded == "invalid_match_response"
        assert outcome.decision is None
        cursor = await conn.execute(
            "SELECT status, error_kind FROM story_matching_runs WHERE id = %s", (run_id,)
        )
        status, error_kind = await cursor.fetchone()
        assert status == "failed"
        assert error_kind == "invalid_match_response"
        cursor = await conn.execute("SELECT count(*) FROM story_match_decisions")
        assert (await cursor.fetchone())[0] == 0
        cursor = await conn.execute("SELECT count(*) FROM story_claims")
        assert (await cursor.fetchone())[0] == 0

    async def test_unparseable_output_fails_run_without_retry(
        self, uow, conn, edition, revision_factory
    ):
        outcome = await self._run_with_raw_output(
            uow, conn, edition, revision_factory, "Простите, но JSON у меня не получился."
        )
        await self._assert_terminal_failure(conn, outcome, outcome.run.id)

    async def test_non_enum_assignment_fails_run_without_retry(
        self, uow, conn, edition, revision_factory
    ):
        outcome = await self._run_with_raw_output(
            uow, conn, edition, revision_factory, '{"assignment": "IRRELEVANT"}'
        )
        await self._assert_terminal_failure(conn, outcome, outcome.run.id)


@pytest.mark.postgres
class TestEmptyCandidateSetFlow:
    async def test_zero_candidates_consult_matcher_and_land_new_story(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)
        # No stories seeded at all: retrieval must freeze an EMPTY candidate
        # set and still hand the claim to the matcher.
        matcher = _FixedMatcher({"assignment": "NEW_STORY"})

        outcome = await _service(uow, matcher).run(claim.id, policy.id, embedding_id)

        assert len(matcher.calls) == 1
        seen_claim, seen_views = matcher.calls[0]
        assert seen_claim.id == claim.id
        assert seen_views == []
        assert outcome.revision is not None and outcome.revision.revision_no == 1
        story = await STORY_REPO.get(conn, outcome.story_id)
        assert story is not None and story.lifecycle_state == "active"
        cursor = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s", (claim.id,)
        )
        assert (await cursor.fetchone())[0] == outcome.story_id
        jobs = await _deferred_jobs(pool, EMBED_REVISION_TASK)
        assert [int(job["args"]["story_revision_id"]) for job in jobs] == [outcome.revision.id]
        assert await _runs_status(conn, outcome.run.id) == "succeeded"

    async def test_empty_candidate_set_frozen_durably_across_retries(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)

        # 1. Matcher on first attempt raises an error during matching after retrieval freeze
        class _FailingMatcher:
            def __init__(self) -> None:
                self.calls = []

            async def choose(self, claim, views, *, edition_name=None):
                self.calls.append((claim, list(views)))
                raise RuntimeError("Simulated transient failure after candidates frozen")

        failing_matcher = _FailingMatcher()
        with pytest.raises(RuntimeError, match="Simulated transient failure"):
            await _service(uow, failing_matcher).run(claim.id, policy.id, embedding_id)

        assert len(failing_matcher.calls) == 1
        assert failing_matcher.calls[0][1] == []  # Empty candidate views

        # Check run is still running and candidates_retrieved_at is sealed
        from src.repositories.story_candidates import StoryMatchingRunRepository

        runs_repo = StoryMatchingRunRepository()
        async with uow.transaction() as db:
            run = await runs_repo.latest_running(db, claim_id=claim.id, policy_id=policy.id)
            assert run is not None
            assert run.candidates_retrieved_at is not None

        # 2. Now a new story is created in the database that would match lexically
        await _seed_story(conn, edition.id, semantic_text=claim.assertion_text)

        # 3. Retry matching on the same run
        successful_matcher = _FixedMatcher({"assignment": "NEW_STORY"})
        outcome = await _service(uow, successful_matcher).run(claim.id, policy.id, embedding_id)

        assert len(successful_matcher.calls) == 1
        # It MUST still see the empty frozen candidate set from the first retrieval, NOT the newly created story!
        seen_claim, seen_views = successful_matcher.calls[0]
        assert seen_views == []
        assert outcome.run.id == run.id
        assert await _runs_status(conn, outcome.run.id) == "succeeded"


@pytest.mark.postgres
class TestConcurrentWinnerConvergence:
    """Scenario 11's actual race branch: a competing execution wins
    uq_story_match_success between our freeze and our mark_succeeded."""

    def _winner_inserting_matcher(
        self, uow, edition_id: int, policy_id: int, claim_id: int, embedding_id: int
    ):
        class _WinnerInsertingMatcher:
            """Plants the concurrent winner during boundary two."""

            def __init__(self):
                self.winner_run_id: int | None = None

            async def choose(self, claim_arg, views, *, edition_name=None):
                del claim_arg, views, edition_name
                async with uow.transaction() as db:
                    cursor = await db.execute(
                        """
                        INSERT INTO story_matching_runs (
                            claim_id, edition_id, policy_id, claim_embedding_id, status
                        )
                        VALUES (%s, %s, %s, %s, 'running')
                        RETURNING id
                        """,
                        (claim_id, edition_id, policy_id, embedding_id),
                    )
                    self.winner_run_id = (await cursor.fetchone())[0]
                    await db.execute(
                        """
                        UPDATE story_matching_runs
                        SET status = 'succeeded', completed_at = now()
                        WHERE id = %s
                        """,
                        (self.winner_run_id,),
                    )
                # Our own execution would have created a phantom NEW_STORY
                # had the canonical guard not rolled the apply back.
                return MatchProposal.from_dict({"assignment": "NEW_STORY"})

        return _WinnerInsertingMatcher()

    async def test_unique_violation_converges_on_concurrent_winner_without_duplicates(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)
        baseline_stories = await _fetch_scalar(conn, "SELECT count(*) FROM stories")
        matcher = self._winner_inserting_matcher(uow, edition.id, policy.id, claim.id, embedding_id)

        outcome = await _service(uow, matcher).run(claim.id, policy.id, embedding_id)

        assert outcome.replayed is True
        assert outcome.story_id is None
        assert outcome.run is not None and outcome.run.id == matcher.winner_run_id
        # Exactly one successful run for the key — the winner's.
        succeeded = await conn.execute(
            """
            SELECT id FROM story_matching_runs
            WHERE claim_id = %s AND policy_id = %s AND status = 'succeeded'
            """,
            (claim.id, policy.id),
        )
        rows = await succeeded.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == matcher.winner_run_id
        # The losing execution's phantom story was rolled back entirely.
        assert await _fetch_scalar(conn, "SELECT count(*) FROM stories") == baseline_stories
        assert await _fetch_scalar(conn, "SELECT count(*) FROM story_claims") == 0

    async def test_vanished_winner_after_index_race_raises_runtime_error(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app, monkeypatch
    ):
        from src.repositories.story_candidates import StoryMatchingRunRepository

        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)
        matcher = self._winner_inserting_matcher(uow, edition.id, policy.id, claim.id, embedding_id)

        async def _winner_never_visible(self_repo, db_conn, *, claim_id, policy_id):
            del self_repo, db_conn, claim_id, policy_id
            return None

        monkeypatch.setattr(StoryMatchingRunRepository, "find_succeeded", _winner_never_visible)

        with pytest.raises(RuntimeError, match="no succeeded run exists"):
            await _service(uow, matcher).run(claim.id, policy.id, embedding_id)

        # The losing apply stayed rolled back; no partial artifacts leaked.
        assert await _fetch_scalar(conn, "SELECT count(*) FROM stories") == 0
        assert await _fetch_scalar(conn, "SELECT count(*) FROM story_claims") == 0


# ---------------------------------------------------------------------------
# Deliverable 2: embed_claim success txn hands off to match_claim atomically
# ---------------------------------------------------------------------------


class _RecordingEmbedProvider:
    def __init__(self):
        self.calls: list[str] = []

    async def embed(self, text, *, purpose, model, dimensions):
        del purpose, model
        self.calls.append(text)
        return [0.25] * dimensions


def _handoff_service(uow, provider) -> EmbeddingService:
    return EmbeddingService(uow=uow, provider=provider, matching_handoff=True)


@pytest.mark.postgres
class TestEmbedClaimDefersMatchClaim:
    async def test_insert_path_creates_policy_and_defers_match_claim_atomically(
        self, uow, pool, conn, edition, revision, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        claim = await _make_claim(conn, edition.id, revision.id)
        service = _handoff_service(uow, _RecordingEmbedProvider())

        embedding_id = await service.embed_claim(claim.id, model=_MODEL_A, dimensions=2)

        assert embedding_id is not None
        cursor = await conn.execute(
            """
            SELECT embedding_model, embedding_dimensions, version
            FROM story_matching_policy_versions WHERE edition_id = %s
            """,
            (edition.id,),
        )
        policy_row = await cursor.fetchone()
        assert policy_row is not None
        assert (policy_row[0], policy_row[1], policy_row[2]) == (_MODEL_A, 2, 1)
        jobs = await _deferred_jobs(pool, MATCH_TASK)
        assert len(jobs) == 1
        args = jobs[0]["args"]
        assert int(args["claim_id"]) == claim.id
        assert int(args["policy_id"]) > 0
        assert int(args["claim_embedding_id"]) == embedding_id
        assert jobs[0]["lock"] == f"story-matching-edition:{edition.id}"

    async def test_reuse_path_defers_the_same_handoff(
        self, uow, pool, conn, edition, revision, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        claim = await _make_claim(conn, edition.id, revision.id)
        provider = _RecordingEmbedProvider()
        service = _handoff_service(uow, provider)

        first_id = await service.embed_claim(claim.id, model=_MODEL_A, dimensions=2)
        second_id = await service.embed_claim(claim.id, model=_MODEL_A, dimensions=2)

        assert first_id == second_id
        assert len(provider.calls) == 1  # reused, no second provider call
        cursor = await conn.execute("SELECT count(*) FROM claim_embeddings")
        assert (await cursor.fetchone())[0] == 1
        # Both visibility paths handed off; duplicates converge downstream.
        jobs = await _deferred_jobs(pool, MATCH_TASK)
        assert len(jobs) == 2
        assert {int(job["args"]["claim_embedding_id"]) for job in jobs} == {first_id}

    async def test_default_service_does_not_hand_off(self, uow, conn, edition, revision):
        claim = await _make_claim(conn, edition.id, revision.id)
        service = EmbeddingService(uow=uow, provider=_RecordingEmbedProvider())

        embedding_id = await service.embed_claim(claim.id, model=_MODEL_A, dimensions=2)

        assert embedding_id is not None
        cursor = await conn.execute(
            "SELECT count(*) FROM story_matching_policy_versions WHERE edition_id = %s",
            (edition.id,),
        )
        assert (await cursor.fetchone())[0] == 0

    async def test_exploding_match_defer_rolls_back_embedding_and_policy(
        self, uow, pool, conn, edition, revision, production_jobs_app, monkeypatch
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        claim = await _make_claim(conn, edition.id, revision.id)

        class _ExplodingDefer:
            def configure(self, **_kwargs):
                return self

            async def defer_async(self, **_kwargs):
                raise RuntimeError("match defer exploded")

        monkeypatch.setattr(jobs_processing, "match_claim", _ExplodingDefer())

        with pytest.raises(RuntimeError, match="match defer exploded"):
            await _handoff_service(uow, _RecordingEmbedProvider()).embed_claim(
                claim.id, model=_MODEL_A, dimensions=2
            )

        async with pool.connection() as observer:
            embeddings = await _fetch_scalar(observer, "SELECT count(*) FROM claim_embeddings")
            policies = await _fetch_scalar(
                observer,
                "SELECT count(*) FROM story_matching_policy_versions WHERE edition_id = %s",
                (edition.id,),
            )
            jobs = await _fetch_scalar(
                observer,
                f"SELECT count(*) FROM procrastinate.procrastinate_jobs "
                f"WHERE task_name = '{MATCH_TASK}'",
            )
        assert embeddings == 0
        assert policies == 0
        assert jobs == 0


# ---------------------------------------------------------------------------
# Plan 3 Task 8: place/entity candidate streams + matching prerequisite barrier
# ---------------------------------------------------------------------------

from src.domain.places import ClaimPlaceMention  # noqa: E402
from src.processing.places import PlaceResolutionPolicyService  # noqa: E402
from src.repositories.places import (  # noqa: E402
    PlaceRepository,
    PlaceResolutionRunRepository,
)
from src.repositories.story_candidates import (  # noqa: E402
    ENTITY_LIMIT,
    LOCATION_OVERLAP_EXACT,
    LOCATION_OVERLAP_WITHIN,
    PLACE_LIMIT,
    REASON_ENTITY,
    REASON_PLACE,
)

_PLACE_REPO_T8 = PlaceRepository()
_PLACE_POLICY_SERVICE_T8 = PlaceResolutionPolicyService()
_T8_RUN_REPO = PlaceResolutionRunRepository()

_T8_COUNTER = {"n": 100}


async def _t8_claim(conn: psycopg.AsyncConnection, edition_id: int, source_item_revision_id: int):
    _T8_COUNTER["n"] += 1
    n = _T8_COUNTER["n"]
    relevance_policy = await _RELEVANCE_POLICY_REPO.insert(
        conn,
        edition_id=edition_id,
        version=n,
        config_hash=f"relevance-cfg-{n}",
        prompt_version="relevance-prompt-1",
    )
    decision = await _DECISION_REPO.insert_root(
        conn,
        source_item_revision_id=source_item_revision_id,
        edition_id=edition_id,
        relevance_policy_id=relevance_policy.id,
        status="relevant",
        confidence=None,
        reason="test setup",
    )
    extraction_policy = await _EXTRACTION_POLICY_REPO.insert(
        conn,
        edition_id=edition_id,
        version=n,
        config_hash=f"extraction-cfg-{n}",
        prompt_version="extraction-prompt-1",
    )
    run, _created = await _RUN_REPO.get_or_create_run(
        conn,
        source_item_revision_id=source_item_revision_id,
        edition_id=edition_id,
        extraction_policy_id=extraction_policy.id,
        relevance_decision_id=decision.id,
    )
    assert await _RUN_REPO.mark_succeeded(conn, run.id, completed_at=_T0)
    assertion = f"Утверждение Т8 номер {n}: вода пришла на улицу Приморскую в АКЗ."
    claims = await _CLAIM_REPO.insert_claims(
        conn,
        run=run,
        claims=[NewClaim(assertion_text=assertion, normalized_assertion=assertion.lower())],
    )
    return claims[0]


async def _t8_place(
    conn: psycopg.AsyncConnection,
    *,
    canonical_name: str,
    parent_place_id: int | None = None,
    aliases: tuple[str, ...] = (),
):
    place = await _PLACE_REPO_T8.insert_place(
        conn, canonical_name=canonical_name, parent_place_id=parent_place_id
    )
    for alias in aliases:
        await _PLACE_REPO_T8.insert_alias(conn, place_id=place.id, alias=alias)
    return place


async def _t8_mention(
    conn: psycopg.AsyncConnection, claim_id: int, original_text: str
) -> ClaimPlaceMention:
    mention, _created = await _PLACE_REPO_T8.create_mention(
        conn, claim_id=claim_id, original_text=original_text
    )
    return mention


async def _t8_resolve(
    uow,
    *,
    mention_id: int,
    edition_id: int,
    policy_id: int,
    place_id: int | None,
) -> None:
    async with uow.transaction() as db:
        run = await _T8_RUN_REPO.insert_running(
            db, mention_id=mention_id, edition_id=edition_id, policy_id=policy_id
        )
        await _PLACE_REPO_T8.insert_resolution_result(
            db,
            run_id=run.id,
            mention_id=mention_id,
            policy_id=policy_id,
            place_id=place_id,
            status="resolved" if place_id is not None else "unresolved",
            reason="test",
        )
        await _T8_RUN_REPO.mark_succeeded(db, run.id, completed_at=_T0)


async def _t8_entity(conn: psycopg.AsyncConnection, claim_id: int, text: str) -> None:
    await _PLACE_REPO_T8.create_entity(conn, claim_id=claim_id, normalized_text=text)


@pytest.mark.postgres
class TestPlaceAndEntityCandidateStreams:
    async def test_within_hierarchy_gives_positive_location_overlap(
        self, uow, conn, edition, revision_factory
    ):
        city = await _t8_place(conn, canonical_name="Бердянск")
        district = await _t8_place(conn, canonical_name="АКЗ", parent_place_id=city.id)
        street = await _t8_place(conn, canonical_name="Приморская", parent_place_id=district.id)
        claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        street_story_claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        district_story_claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)

        exact_story = await _seed_story(
            conn,
            edition.id,
            semantic_text="Совершенно непохожий текст без общих слов с утверждением.",
        )
        within_story = await _seed_story(
            conn,
            edition.id,
            semantic_text="Ещё один лексически далёкий текст про район города.",
        )
        policy = await _insert_policy(
            conn, edition.id, vector_limit=0, lexical_limit=0, state_fallback_limit=0
        )

        street_mention = await _t8_mention(conn, claim.id, "Приморская")
        exact_claim_mention = await _t8_mention(conn, street_story_claim.id, "Приморская")
        district_claim_mention = await _t8_mention(conn, district_story_claim.id, "АКЗ")
        place_policy = await _PLACE_POLICY_SERVICE_T8.ensure_current(conn, edition_id=edition.id)
        await _t8_resolve(
            uow,
            mention_id=street_mention.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
            place_id=street.id,
        )
        await _t8_resolve(
            uow,
            mention_id=exact_claim_mention.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
            place_id=street.id,
        )
        await _t8_resolve(
            uow,
            mention_id=district_claim_mention.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
            place_id=district.id,
        )
        # Stories reach the place stream THROUGH their attached claims.
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id) VALUES (%s, %s)",
            (exact_story.story_id, street_story_claim.id),
        )
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id) VALUES (%s, %s)",
            (within_story.story_id, district_story_claim.id),
        )

        candidates = await _retrieve(uow, claim, policy)

        by_story = {c.story_id: c for c in candidates}
        # Street ⊂ district WITHIN relation admits the district story with a
        # positive heuristic signal — never a threshold, only provenance.
        assert by_story[within_story.story_id].location_overlap == pytest.approx(
            LOCATION_OVERLAP_WITHIN
        )
        assert REASON_PLACE in by_story[within_story.story_id].retrieval_reasons
        # The same resolved place is an exact hit worth the full signal.
        assert by_story[exact_story.story_id].location_overlap == pytest.approx(
            LOCATION_OVERLAP_EXACT
        )

    async def test_place_stream_alone_admits_story_missed_by_other_streams(
        self, uow, conn, edition, revision_factory
    ):
        city = await _t8_place(conn, canonical_name="Бердянск")
        district = await _t8_place(conn, canonical_name="Слободка", parent_place_id=city.id)
        claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        other_claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        hidden_story = await _seed_story(
            conn,
            edition.id,
            semantic_text="Транспортная развязка открыта для движения.",
        )
        policy = await _insert_policy(
            conn,
            edition.id,
            vector_limit=0,
            lexical_limit=10,
            state_fallback_limit=0,
        )
        mention = await _t8_mention(conn, claim.id, "Слобідка")
        other_mention = await _t8_mention(conn, other_claim.id, "Слободка")
        place_policy = await _PLACE_POLICY_SERVICE_T8.ensure_current(conn, edition_id=edition.id)
        await _t8_resolve(
            uow,
            mention_id=mention.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
            place_id=district.id,
        )
        await _t8_resolve(
            uow,
            mention_id=other_mention.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
            place_id=district.id,
        )
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id) VALUES (%s, %s)",
            (hidden_story.story_id, other_claim.id),
        )

        candidates = await _retrieve(uow, claim, policy)

        # Vector/state limits are zero and no token overlaps lexically, so ONLY
        # the place stream can have admitted this frozen candidate.
        hits = [c for c in candidates if c.story_id == hidden_story.story_id]
        assert len(hits) == 1
        assert hits[0].retrieval_reasons == frozenset({REASON_PLACE})
        assert hits[0].vector_distance is None and hits[0].lexical_score is None

    async def test_entity_stream_records_fraction_overlap_provenance(
        self, uow, conn, edition, revision_factory
    ):
        claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        water_story = await _seed_story(
            conn,
            edition.id,
            semantic_text="Водоканал восстановил подачу воды частично.",
        )
        unrelated_story = await _seed_story(
            conn, edition.id, semantic_text="Автобусный маршрут изменён."
        )
        policy = await _insert_policy(conn, edition.id, vector_limit=0, state_fallback_limit=0)
        await _t8_entity(conn, claim.id, "водоканал")
        await _t8_entity(conn, claim.id, "приморская")

        candidates = await _retrieve(uow, claim, policy)

        by_story = {c.story_id: c for c in candidates}
        hit = by_story[water_story.story_id]
        assert REASON_ENTITY in hit.retrieval_reasons
        # One of two normalized entities present in the story document → 0.5.
        assert hit.entity_overlap == pytest.approx(0.5)
        assert unrelated_story.story_id not in by_story

    def test_place_and_entity_limit_constants_are_pinned_documentation(self):
        """Documentation pin: PLACE_LIMIT/ENTITY_LIMIT are module constants
        (versioned-policy candidates for a later migration), pinned here so
        an accidental change is a visible decision."""
        assert PLACE_LIMIT == 10
        assert ENTITY_LIMIT == 10

    async def test_frozen_place_only_candidate_row_carries_explicit_provenance(
        self, uow, conn, edition, revision_factory
    ):
        from src.repositories.story_candidates import StoryMatchingRunRepository

        city = await _t8_place(conn, canonical_name="Бердянск")
        district = await _t8_place(conn, canonical_name="Слобідка", parent_place_id=city.id)
        claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        other_claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        hidden_story = await _seed_story(
            conn, edition.id, semantic_text="Ярмарка у ратуши открыта для всех."
        )
        policy = await _insert_policy(
            conn, edition.id, vector_limit=0, lexical_limit=10, state_fallback_limit=0
        )
        mention = await _t8_mention(conn, claim.id, "Слободка")
        other_mention = await _t8_mention(conn, other_claim.id, "Слобідка")
        place_policy = await _PLACE_POLICY_SERVICE_T8.ensure_current(conn, edition_id=edition.id)
        await _t8_resolve(
            uow,
            mention_id=mention.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
            place_id=district.id,
        )
        await _t8_resolve(
            uow,
            mention_id=other_mention.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
            place_id=district.id,
        )
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id) VALUES (%s, %s)",
            (hidden_story.story_id, other_claim.id),
        )

        # Freeze boundary one exactly like the orchestrator, then re-read the
        # persisted candidate ROWS.
        runs_repo = StoryMatchingRunRepository()
        async with uow.transaction() as db:
            run = await runs_repo.insert_running(
                db,
                claim_id=claim.id,
                edition_id=edition.id,
                policy_id=policy.id,
                claim_embedding_id=None,
            )
            retrieved = await _RETRIEVER.retrieve(
                db, claim=claim, claim_embedding=[1.0, 0.0], policy=policy
            )
            await runs_repo.save_candidates(db, run_id=run.id, candidates=retrieved)
            frozen = await runs_repo.frozen_candidates(db, run.id)

        hits = [c for c in frozen if c.story_id == hidden_story.story_id]
        assert len(hits) == 1
        hit = hits[0]
        # The admitted stream is persisted EXPLICITLY on the row...
        assert hit.retrieved_by_place is True
        assert hit.retrieved_by_vector is False
        assert hit.retrieved_by_lexical is False
        assert hit.retrieved_by_state is False
        assert hit.retrieved_by_entity is False
        # ...and the score-non-null invariant holds for that stream.
        assert hit.location_overlap is not None

    async def test_place_only_hint_never_forces_same_story(
        self, uow, pool, conn, edition, revision_factory
    ):
        city = await _t8_place(conn, canonical_name="Бердянск")
        district = await _t8_place(conn, canonical_name="Коса", parent_place_id=city.id)
        claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        other_claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        target = await _seed_story(
            conn,
            edition.id,
            semantic_text="Совершенно иной смысл: концерт на набережной.",
        )
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_policy(conn, edition.id)
        mention = await _t8_mention(conn, claim.id, "Коса")
        other_mention = await _t8_mention(conn, other_claim.id, "Коса")
        place_policy = await _PLACE_POLICY_SERVICE_T8.ensure_current(conn, edition_id=edition.id)
        await _t8_resolve(
            uow,
            mention_id=mention.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
            place_id=district.id,
        )
        await _t8_resolve(
            uow,
            mention_id=other_mention.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
            place_id=district.id,
        )
        # The matcher refuses to merge despite the shared place hint.
        matcher = _FixedMatcher({"assignment": "NEW_STORY"})

        outcome = await _service(uow, matcher).run(claim.id, policy.id, embedding_id)

        assert outcome.story_id is not None and outcome.story_id != target.story_id
        cursor = await conn.execute(
            "SELECT assignment FROM story_match_decisions WHERE run_id = %s",
            (outcome.run.id,),
        )
        assert (await cursor.fetchone())[0] == "NEW_STORY"
        # The soft-signal rule is pinned in the matcher contract itself:
        # retrieval hints are provenance metadata, never evidence, so a place
        # overlap alone can never force SAME_STORY.
        from src.processing.story_matching import StoryMatcher

        prompt_text = StoryMatcher._system_prompt(None, None)
        assert "never evidence" in prompt_text

    async def test_retriever_hints_carry_location_and_entity_scores(self):
        view = MatcherCandidateView(
            candidate=SimpleNamespace(
                story_id=1,
                story_revision_id=11,
                retrieval_reasons=frozenset({REASON_PLACE}),
                vector_distance=None,
                lexical_score=None,
                location_overlap=LOCATION_OVERLAP_EXACT,
                entity_overlap=0.5,
            ),
            revision=_revision_row(11, 1, "текст"),
        )
        assert view.hints["location_overlap"] == LOCATION_OVERLAP_EXACT
        assert view.hints["entity_overlap"] == pytest.approx(0.5)


@pytest.mark.postgres
class TestStoryMatchingPrerequisiteBarrier:
    """maybe_schedule(): queue match_claim ONLY when a compatible claim
    embedding exists AND every place mention holds a current-policy result."""

    @pytest.fixture(autouse=True)
    def _install_runtime(self, uow, pool, production_jobs_app):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )

    async def _prerequisite(self):
        from src.processing.story_matching import StoryMatchingPrerequisiteService

        return StoryMatchingPrerequisiteService()

    async def test_no_compatible_embedding_returns_false_without_deferring(
        self, uow, pool, conn, edition, revision_factory
    ):
        claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        prerequisite = await self._prerequisite()

        async with uow.transaction() as db:
            scheduled = await prerequisite.maybe_schedule(db, claim_id=claim.id)

        assert scheduled is False
        assert await _deferred_jobs(pool, MATCH_TASK) == []

    async def test_missing_result_blocks_matching(self, uow, pool, conn, edition, revision_factory):
        claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        await _seed_claim_embedding(uow, claim.id)
        await _t8_mention(conn, claim.id, "неразрешённое место")
        prerequisite = await self._prerequisite()

        async with uow.transaction() as db:
            scheduled = await prerequisite.maybe_schedule(db, claim_id=claim.id)

        assert scheduled is False
        assert await _deferred_jobs(pool, MATCH_TASK) == []

    async def test_explicit_unresolved_result_satisfies_barrier_and_defers_once(
        self, uow, pool, conn, edition, revision_factory
    ):
        claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        mention = await _t8_mention(conn, claim.id, "неизвестный хутор")
        place_policy = await _PLACE_POLICY_SERVICE_T8.ensure_current(conn, edition_id=edition.id)
        await _t8_resolve(
            uow,
            mention_id=mention.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
            place_id=None,
        )
        prerequisite = await self._prerequisite()

        async with uow.transaction() as db:
            scheduled = await prerequisite.maybe_schedule(db, claim_id=claim.id)

        assert scheduled is True
        jobs = await _deferred_jobs(pool, MATCH_TASK)
        assert len(jobs) == 1
        args = jobs[0]["args"]
        assert int(args["claim_id"]) == claim.id
        assert int(args["claim_embedding_id"]) == embedding_id
        assert int(args["policy_id"]) > 0
        assert jobs[0]["lock"] == f"story-matching-edition:{edition.id}"

    async def test_no_mentions_schedules_immediately(
        self, uow, pool, conn, edition, revision_factory
    ):
        claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        prerequisite = await self._prerequisite()

        async with uow.transaction() as db:
            scheduled = await prerequisite.maybe_schedule(db, claim_id=claim.id)

        assert scheduled is True
        jobs = await _deferred_jobs(pool, MATCH_TASK)
        assert len(jobs) == 1
        assert int(jobs[0]["args"]["claim_embedding_id"]) == embedding_id

    async def test_barrier_opens_only_after_last_resolution(
        self, uow, pool, conn, edition, revision_factory
    ):
        claim = await _t8_claim(conn, edition.id, (await revision_factory()).id)
        await _seed_claim_embedding(uow, claim.id)
        first = await _t8_mention(conn, claim.id, "АКЗ")
        second = await _t8_mention(conn, claim.id, "Коса")
        place_policy = await _PLACE_POLICY_SERVICE_T8.ensure_current(conn, edition_id=edition.id)
        district = await _t8_place(conn, canonical_name="АКЗ", aliases=("АКЗ",))
        kosa = await _t8_place(conn, canonical_name="Коса", aliases=("Коса",))
        await _t8_resolve(
            uow,
            mention_id=first.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
            place_id=district.id,
        )
        prerequisite = await self._prerequisite()

        async with uow.transaction() as db:
            partially = await prerequisite.maybe_schedule(db, claim_id=claim.id)
        assert partially is False

        await _t8_resolve(
            uow,
            mention_id=second.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
            place_id=kosa.id,
        )
        async with uow.transaction() as db:
            fully = await prerequisite.maybe_schedule(db, claim_id=claim.id)
        assert fully is True
        assert len(await _deferred_jobs(pool, MATCH_TASK)) == 1


@pytest.mark.postgres
class TestKnowledgeNoEmbeddingsMode:
    """Tests for zero-external-embedding execution in knowledge_no_embeddings mode."""

    async def test_prerequisite_schedules_without_embedding(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        from src.processing.story_matching import StoryMatchingPrerequisiteService

        prereq = StoryMatchingPrerequisiteService()

        # In default knowledge_full mode without embedding -> returns False
        async with uow.transaction() as db:
            scheduled = await prereq.maybe_schedule(
                db, claim_id=claim.id, processing_mode="knowledge_full"
            )
        assert scheduled is False

        # In knowledge_no_embeddings mode -> returns True and defers match_claim with claim_embedding_id=None
        async with uow.transaction() as db:
            scheduled = await prereq.maybe_schedule(
                db, claim_id=claim.id, processing_mode="knowledge_no_embeddings"
            )
        assert scheduled is True

        jobs = await _deferred_jobs(pool, MATCH_TASK)
        assert len(jobs) == 1
        assert jobs[0]["args"]["claim_id"] == claim.id
        assert jobs[0]["args"]["claim_embedding_id"] is None

    async def test_matching_runs_and_retrieves_lexically_without_vector(
        self, uow, conn, edition, revision_factory, production_jobs_app
    ):
        # 1. Seed an existing story with clear lexical keywords
        seeded = await _seed_story(
            conn,
            edition.id,
            title="Водоканал Бердянска",
            summary="Ремонт водопровода на Восточном проспекте",
            semantic_text="Водоканал Бердянска ведет ремонт водопровода на Восточном проспекте",
        )
        story_id = seeded.story_id

        # 2. Make a claim sharing lexical tokens
        rev = await revision_factory()
        claim = await _make_claim(
            conn,
            edition.id,
            rev.id,
            assertion="Водоканал Бердянска сообщил об отключении воды на Восточном",
        )

        # 3. Create a policy for knowledge_no_embeddings (embedding_model="none", dimensions=0)
        from src.processing.story_matching import StoryMatchingPolicyService

        policy_svc = StoryMatchingPolicyService()
        async with uow.transaction() as db:
            policy = await policy_svc.ensure_current(
                db,
                edition_id=edition.id,
                embedding_model="none",
                embedding_dimensions=0,
            )

        # 4. Run StoryMatchingService with claim_embedding_id=None
        matcher = _FixedMatcher({"assignment": "SAME_STORY", "target_story_id": story_id})
        svc = _service(uow, matcher)
        outcome = await svc.run(claim.id, policy.id, claim_embedding_id=None)

        from src.repositories.story_candidates import StoryMatchingRunRepository

        assert outcome.replayed is False
        assert await _runs_status(conn, outcome.run.id) == "succeeded"
        async with uow.transaction() as db:
            refreshed_run = await StoryMatchingRunRepository().get(db, outcome.run.id)
            assert refreshed_run is not None
            assert refreshed_run.retrieval_mode == "knowledge_no_embeddings"
            assert refreshed_run.claim_embedding_id is None

        # Verify candidate was retrieved via lexical recall
        seen_claim, views = matcher.calls[0]
        assert len(views) >= 1
        matching_view = next((v for v in views if v.candidate.story_id == story_id), None)
        assert matching_view is not None
        assert matching_view.candidate.retrieved_by_lexical is True
        assert matching_view.candidate.retrieved_by_vector is False
        assert matching_view.candidate.vector_distance is None
