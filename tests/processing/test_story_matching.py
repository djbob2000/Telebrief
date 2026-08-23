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


async def _make_claim(conn: psycopg.AsyncConnection, edition_id: int, source_item_revision_id: int):
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
    assertion = f"Утверждение номер {n}: вода пришла на улицу Приморскую."
    claims = await _CLAIM_REPO.insert_claims(
        conn,
        run=run,
        claims=[
            NewClaim(
                assertion_text=assertion,
                normalized_assertion=assertion.lower(),
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
