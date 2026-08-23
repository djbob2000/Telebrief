"""Plan 3 Task 5: semantic Claim/StoryRevision embeddings + exact pgvector retrieval.

Global rules under test (spec §17 rulings):

* ONE immutable embedding row per (semantic object, model, dimensions,
  purpose, content_hash); the embedding input is ALWAYS the complete
  ``Claim.normalized_assertion`` / complete ``StoryRevision.semantic_text``
  and NEVER raw revision text, per-sentence slices, or sliding chunks.
* A returned vector whose length differs from the queued ``dimensions`` is
  rejected as a typed error and nothing persists.
* Retrieval is EXACT pgvector cosine (``<=>``) over model+dimension-filtered
  rows; no ANN index exists.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import psycopg
import pytest

from src.domain.claims import NewClaim
from src.embedding_providers import (
    EmbeddingDimensionMismatch,
    EmbeddingProviderError,
    GoogleGeminiEmbeddingProvider,
)
from src.processing.embeddings import (
    EmbeddingInputBuilder,
    EmbeddingService,
)
from src.repositories.claims import (
    ClaimExtractionPolicyRepository,
    ClaimExtractionRunRepository,
    ClaimRepository,
)
from src.repositories.embeddings import (
    PURPOSE_CLAIM_QUERY,
    PURPOSE_STORY_DOCUMENT,
    EmbeddingRepository,
    StoryVectorCandidate,
)
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
    RelevancePolicyVersionRepository,
)

logger = logging.getLogger(__name__)

MODEL_A = "test-embedding-a"
MODEL_B = "test-embedding-b"

AKZ_NORMALIZED = "Водоснабжение на АКЗ ещё не восстановилось."
CONTEXT_REPLY_RAW = "Всё ещё нет."
SHORT_CLAIM = "Пожар у вокзала."

_SEVEN_SENTENCES = (
    "Утром жители сообщили о проблеме. "
    "Вода пропала в шести домах частного сектора. "
    "Аварийная бригада прибыла к восьми часам. "
    "Повреждение нашли на вводе в один из домов. "
    "К полудню подача возобновилась частично. "
    "Полное восстановление обещали к вечеру. "
    "Жители ждут официальных разъяснений."
)

_RUN_REPO = ClaimExtractionRunRepository()
_CLAIM_REPO = ClaimRepository()
_POLICY_REPO = ClaimExtractionPolicyRepository()

_seed_counters = {"policy": 0}


class RecordingEmbeddingProvider:
    """Minimal EmbeddingProvider double: records every call verbatim."""

    def __init__(self, *, vector_factory=None):
        self.calls: list[tuple[str, str, str, int]] = []
        self._vector_factory = vector_factory

    def _vector(self, dimensions: int) -> list[float]:
        if self._vector_factory is not None:
            return self._vector_factory(dimensions)
        return [0.25] * dimensions

    async def embed(self, text, *, purpose, model, dimensions) -> list[float]:
        self.calls.append((text, purpose, model, dimensions))
        return self._vector(dimensions)


async def _seed_claim(
    conn: psycopg.AsyncConnection,
    edition_id: int,
    revision_id: int,
    *,
    normalized: str,
    assertion: str | None = None,
) -> object:
    """One extracted claim with full spec §15 provenance chain."""
    _seed_counters["policy"] += 1
    n = _seed_counters["policy"]
    relevance_policy = await RelevancePolicyVersionRepository().insert(
        conn,
        edition_id=edition_id,
        version=n,
        config_hash=f"rel-cfg-{n}",
        prompt_version="rel-prompt",
    )
    decision = await EditionRelevanceDecisionRepository().insert_root(
        conn,
        source_item_revision_id=revision_id,
        edition_id=edition_id,
        relevance_policy_id=relevance_policy.id,
        status="relevant",
        confidence=None,
        reason="test setup",
    )
    extraction_policy = await _POLICY_REPO.insert(
        conn,
        edition_id=edition_id,
        version=n,
        config_hash=f"claim-cfg-{n}",
        prompt_version="claim-prompt",
    )
    run, _created = await _RUN_REPO.get_or_create_run(
        conn,
        source_item_revision_id=revision_id,
        edition_id=edition_id,
        extraction_policy_id=extraction_policy.id,
        relevance_decision_id=decision.id,
    )
    inserted = await _CLAIM_REPO.insert_claims(
        conn,
        run=run,
        claims=[
            NewClaim(
                assertion_text=assertion if assertion is not None else normalized,
                normalized_assertion=normalized,
            )
        ],
    )
    return inserted[0]


async def _seed_story_revision(
    conn: psycopg.AsyncConnection,
    edition_id: int,
    *,
    semantic_text: str,
    lifecycle_state: str = "active",
    created_at: dt.datetime | None = None,
) -> SimpleNamespace:
    """One story with a single revision wired as its current revision."""
    cursor = await conn.execute(
        "INSERT INTO stories (edition_id, lifecycle_state) VALUES (%s, %s) RETURNING id",
        (edition_id, lifecycle_state),
    )
    story_id = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash, created_at
        )
        VALUES (%s, 1, 'open', %s, %s, COALESCE(%s, now()))
        RETURNING id
        """,
        (story_id, semantic_text, f"hash-{story_id}", created_at),
    )
    revision_id = (await cursor.fetchone())[0]
    await conn.execute(
        "UPDATE stories SET current_revision_id = %s WHERE id = %s",
        (revision_id, story_id),
    )
    return SimpleNamespace(story_id=story_id, revision_id=revision_id)


@asynccontextmanager
async def _txn(uow) -> AsyncIterator[psycopg.AsyncConnection]:
    """Committed unit-of-work scope for pgvector-registered pool connections."""
    async with uow.transaction() as conn:
        yield conn


def _service(uow, provider) -> EmbeddingService:
    return EmbeddingService(uow=uow, provider=provider)


# ---------------------------------------------------------------------------
# Input ownership: EmbeddingInputBuilder
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_builder_for_claim_returns_complete_normalized_assertion():
    claim = SimpleNamespace(normalized_assertion=AKZ_NORMALIZED, assertion_text=CONTEXT_REPLY_RAW)
    assert EmbeddingInputBuilder.for_claim(claim) == AKZ_NORMALIZED


@pytest.mark.unit
def test_builder_for_story_revision_returns_complete_semantic_text():
    semantic_text = "Авария на АКЗ устранена; водоснабжение полностью восстановлено."
    revision = SimpleNamespace(semantic_text=semantic_text)
    assert EmbeddingInputBuilder.for_story_revision(revision) == semantic_text


# ---------------------------------------------------------------------------
# embed_claim: input contract
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestEmbedClaimInputs:
    async def test_sends_entire_normalized_assertion_once_as_query_purpose(
        self, uow, conn, edition, revision
    ):
        claim = await _seed_claim(
            conn,
            edition.id,
            revision.id,
            normalized=AKZ_NORMALIZED,
            assertion=CONTEXT_REPLY_RAW,
        )
        provider = RecordingEmbeddingProvider()

        await _service(uow, provider).embed_claim(claim.id, model=MODEL_A, dimensions=1536)

        assert provider.calls == [(AKZ_NORMALIZED, PURPOSE_CLAIM_QUERY, MODEL_A, 1536)]

    async def test_two_claims_from_one_seven_sentence_source_mean_two_calls(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(text_content=_SEVEN_SENTENCES)
        first = await _seed_claim(
            conn, edition.id, revision.id, normalized="Вода пропала в шести домах частного сектора."
        )
        second = await _seed_claim(
            conn,
            edition.id,
            revision.id,
            normalized="Подача воды возобновилась частично к полудню.",
        )
        provider = RecordingEmbeddingProvider()
        service = _service(uow, provider)

        await service.embed_claim(first.id, model=MODEL_A, dimensions=1536)
        await service.embed_claim(second.id, model=MODEL_A, dimensions=1536)

        assert len(provider.calls) == 2
        assert {call[0] for call in provider.calls} == {
            "Вода пропала в шести домах частного сектора.",
            "Подача воды возобновилась частично к полудню.",
        }
        assert all(call[1] == PURPOSE_CLAIM_QUERY for call in provider.calls)

    async def test_short_complete_claim_is_embedded_unchanged(self, uow, conn, edition, revision):
        claim = await _seed_claim(conn, edition.id, revision.id, normalized=SHORT_CLAIM)
        provider = RecordingEmbeddingProvider()

        await _service(uow, provider).embed_claim(claim.id, model=MODEL_A, dimensions=1536)

        assert provider.calls[0][0] == SHORT_CLAIM
        assert len(provider.calls) == 1

    async def test_raw_context_reply_is_never_the_embedding_input(
        self, uow, conn, edition, revision
    ):
        claim = await _seed_claim(
            conn,
            edition.id,
            revision.id,
            normalized=AKZ_NORMALIZED,
            assertion=CONTEXT_REPLY_RAW,
        )
        provider = RecordingEmbeddingProvider()

        await _service(uow, provider).embed_claim(claim.id, model=MODEL_A, dimensions=1536)

        assert all(CONTEXT_REPLY_RAW != call[0] for call in provider.calls)
        assert provider.calls[0][0] == AKZ_NORMALIZED


# ---------------------------------------------------------------------------
# embed_claim: validation, persistence, reuse
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestEmbedClaimPersistence:
    async def test_wrong_length_vector_rejected_and_not_persisted(
        self, uow, conn, edition, revision
    ):
        claim = await _seed_claim(conn, edition.id, revision.id, normalized=AKZ_NORMALIZED)
        provider = RecordingEmbeddingProvider(vector_factory=lambda dims: [0.5] * (dims // 100))
        service = _service(uow, provider)

        with pytest.raises(EmbeddingDimensionMismatch):
            await service.embed_claim(claim.id, model=MODEL_A, dimensions=1536)

        async with _txn(uow) as db:
            rows = await EmbeddingRepository().list_claim_embedding_ids(
                db, claim_id=claim.id, model=MODEL_A, dimensions=1536
            )
        assert rows == []

    async def test_same_hash_model_purpose_reuses_existing_row(self, uow, conn, edition, revision):
        claim = await _seed_claim(conn, edition.id, revision.id, normalized=AKZ_NORMALIZED)
        provider = RecordingEmbeddingProvider()
        service = _service(uow, provider)

        first_id = await service.embed_claim(claim.id, model=MODEL_A, dimensions=1536)
        second_id = await service.embed_claim(claim.id, model=MODEL_A, dimensions=1536)

        assert first_id == second_id
        assert len(provider.calls) == 1
        cursor = await conn.execute("SELECT count(*) FROM claim_embeddings")
        assert (await cursor.fetchone())[0] == 1

    async def test_new_model_or_dimensions_create_new_rows_never_mutate_old(
        self, uow, conn, edition, revision
    ):
        claim = await _seed_claim(conn, edition.id, revision.id, normalized=AKZ_NORMALIZED)
        provider = RecordingEmbeddingProvider()
        service = _service(uow, provider)

        base_id = await service.embed_claim(claim.id, model=MODEL_A, dimensions=1536)
        other_model_id = await service.embed_claim(claim.id, model=MODEL_B, dimensions=1536)
        other_dims_id = await service.embed_claim(claim.id, model=MODEL_A, dimensions=768)

        assert len({base_id, other_model_id, other_dims_id}) == 3
        cursor = await conn.execute("SELECT count(*) FROM claim_embeddings")
        assert (await cursor.fetchone())[0] == 3
        # The original row is immutable: same id, untouched.
        again = await service.embed_claim(claim.id, model=MODEL_A, dimensions=1536)
        assert again == base_id

    async def test_missing_claim_raises_value_error(self, uow):
        provider = RecordingEmbeddingProvider()
        with pytest.raises(ValueError, match="does not exist"):
            await _service(uow, provider).embed_claim(10**9, model=MODEL_A, dimensions=1536)
        assert provider.calls == []


# ---------------------------------------------------------------------------
# embed_story_revision: input contract
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestEmbedStoryRevision:
    async def test_semantic_text_embedded_once_whole_with_document_purpose(
        self, uow, conn, edition
    ):
        semantic_text = "Водоснабжение на АКЗ восстановлено; вода появилась утром 12 августа."
        seeded = await _seed_story_revision(conn, edition.id, semantic_text=semantic_text)
        provider = RecordingEmbeddingProvider()

        first_id = await _service(uow, provider).embed_story_revision(
            seeded.revision_id, model=MODEL_A, dimensions=1536
        )
        second_id = await _service(uow, provider).embed_story_revision(
            seeded.revision_id, model=MODEL_A, dimensions=1536
        )

        assert first_id == second_id
        assert provider.calls == [(semantic_text, PURPOSE_STORY_DOCUMENT, MODEL_A, 1536)]
        cursor = await conn.execute(
            "SELECT purpose FROM story_revision_embeddings WHERE story_revision_id = %s",
            (seeded.revision_id,),
        )
        assert (await cursor.fetchone())[0] == PURPOSE_STORY_DOCUMENT


# ---------------------------------------------------------------------------
# Exact pgvector retrieval (<=> cosine, no ANN index)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestFindStoryCandidates:
    async def test_orders_by_exact_cosine_distance_with_known_vectors(self, uow, conn, edition):
        north = await _seed_story_revision(conn, edition.id, semantic_text="север")
        diagonal = await _seed_story_revision(conn, edition.id, semantic_text="диагональ")
        east = await _seed_story_revision(conn, edition.id, semantic_text="восток")
        repo = EmbeddingRepository()
        async with _txn(uow) as db:
            await repo.insert_story_revision_embedding(
                db,
                story_revision_id=north.revision_id,
                embedding=[1.0, 0.0],
                model=MODEL_A,
                dimensions=2,
                purpose=PURPOSE_STORY_DOCUMENT,
                content_hash="h-north",
            )
            await repo.insert_story_revision_embedding(
                db,
                story_revision_id=diagonal.revision_id,
                embedding=[0.7071, 0.7071],
                model=MODEL_A,
                dimensions=2,
                purpose=PURPOSE_STORY_DOCUMENT,
                content_hash="h-diag",
            )
            await repo.insert_story_revision_embedding(
                db,
                story_revision_id=east.revision_id,
                embedding=[0.0, 1.0],
                model=MODEL_A,
                dimensions=2,
                purpose=PURPOSE_STORY_DOCUMENT,
                content_hash="h-east",
            )

            candidates = await repo.find_story_candidates(
                db,
                edition_id=edition.id,
                query_embedding=[1.0, 0.0],
                model=MODEL_A,
                dimensions=2,
                limit=10,
            )

        assert isinstance(candidates[0], StoryVectorCandidate)
        assert [c.story_revision_id for c in candidates] == [
            north.revision_id,
            diagonal.revision_id,
            east.revision_id,
        ]
        assert candidates[0].vector_distance == pytest.approx(0.0, abs=1e-6)
        assert candidates[1].vector_distance == pytest.approx(1 - 0.7071, abs=1e-3)
        assert candidates[2].vector_distance == pytest.approx(1.0, abs=1e-6)
        assert candidates[0].embedding_id > 0

    async def test_filters_edition_lifecycle_model_and_dimensions(
        self, uow, conn, edition, second_edition
    ):
        keeper = await _seed_story_revision(conn, edition.id, semantic_text="нужна")
        archived = await _seed_story_revision(
            conn, edition.id, semantic_text="архив", lifecycle_state="archived"
        )
        foreign = await _seed_story_revision(conn, second_edition.id, semantic_text="другой выпуск")
        repo = EmbeddingRepository()
        async with _txn(uow) as db:
            await repo.insert_story_revision_embedding(
                db,
                story_revision_id=keeper.revision_id,
                embedding=[1.0, 0.0],
                model=MODEL_A,
                dimensions=2,
                purpose=PURPOSE_STORY_DOCUMENT,
                content_hash="h-keep",
            )
            await repo.insert_story_revision_embedding(
                db,
                story_revision_id=archived.revision_id,
                embedding=[1.0, 0.0],
                model=MODEL_A,
                dimensions=2,
                purpose=PURPOSE_STORY_DOCUMENT,
                content_hash="h-arch",
            )
            await repo.insert_story_revision_embedding(
                db,
                story_revision_id=foreign.revision_id,
                embedding=[1.0, 0.0],
                model=MODEL_A,
                dimensions=2,
                purpose=PURPOSE_STORY_DOCUMENT,
                content_hash="h-far",
            )
            # Same revision, wrong model space: must be filtered out.
            await repo.insert_story_revision_embedding(
                db,
                story_revision_id=keeper.revision_id,
                embedding=[1.0, 0.0],
                model=MODEL_B,
                dimensions=2,
                purpose=PURPOSE_STORY_DOCUMENT,
                content_hash="h-wrongmodel",
            )

            candidates = await repo.find_story_candidates(
                db,
                edition_id=edition.id,
                query_embedding=[1.0, 0.0],
                model=MODEL_A,
                dimensions=2,
                limit=10,
            )

        assert [c.story_revision_id for c in candidates] == [keeper.revision_id]

    async def test_event_time_bounds_narrow_the_stream(self, uow, conn, edition):
        old = await _seed_story_revision(
            conn,
            edition.id,
            semantic_text="старая история",
            created_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        )
        fresh = await _seed_story_revision(
            conn,
            edition.id,
            semantic_text="свежая история",
            created_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        )
        repo = EmbeddingRepository()
        for revision_id, content_hash in ((old.revision_id, "h-old"), (fresh.revision_id, "h-new")):
            async with _txn(uow) as db:
                await repo.insert_story_revision_embedding(
                    db,
                    story_revision_id=revision_id,
                    embedding=[1.0, 0.0],
                    model=MODEL_A,
                    dimensions=2,
                    purpose=PURPOSE_STORY_DOCUMENT,
                    content_hash=content_hash,
                )
        async with _txn(uow) as db:
            candidates = await repo.find_story_candidates(
                db,
                edition_id=edition.id,
                query_embedding=[1.0, 0.0],
                model=MODEL_A,
                dimensions=2,
                limit=10,
                event_time_start=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
            )

        assert [c.story_revision_id for c in candidates] == [fresh.revision_id]


# ---------------------------------------------------------------------------
# Google Gemini adapter: response validation + request mapping
# ---------------------------------------------------------------------------


class _StubCompatEmbeddingsApi:
    """Duck-typed stand-in for ``AsyncOpenAI(...).embeddings``."""

    def __init__(self, embedding: list[float]):
        self._embedding = embedding
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(embedding=self._embedding)])


def _gemini_provider(
    vector: list[float],
) -> tuple[GoogleGeminiEmbeddingProvider, _StubCompatEmbeddingsApi]:
    provider = GoogleGeminiEmbeddingProvider(api_key="stub-key", logger=logger, timeout=45)
    api = _StubCompatEmbeddingsApi(vector)
    provider.client = SimpleNamespace(embeddings=api)
    return provider, api


@pytest.mark.unit
class TestGoogleGeminiEmbeddingProvider:
    async def test_maps_input_model_dimensions_through_compat_endpoint(self):
        provider, api = _gemini_provider([0.5] * 1536)

        vector = await provider.embed(
            AKZ_NORMALIZED, purpose=PURPOSE_CLAIM_QUERY, model=MODEL_A, dimensions=1536
        )

        assert vector == [0.5] * 1536
        assert api.calls == [{"model": MODEL_A, "input": AKZ_NORMALIZED, "dimensions": 1536}]

    async def test_document_purpose_targets_the_same_endpoint_once(self):
        provider, api = _gemini_provider([0.5] * 768)

        await provider.embed(
            "compact story meaning", purpose=PURPOSE_STORY_DOCUMENT, model=MODEL_B, dimensions=768
        )

        assert len(api.calls) == 1
        assert api.calls[0]["input"] == "compact story meaning"

    async def test_rejects_response_with_wrong_dimensionality(self):
        provider, _api = _gemini_provider([0.5] * 10)

        with pytest.raises(EmbeddingDimensionMismatch):
            await provider.embed(
                AKZ_NORMALIZED, purpose=PURPOSE_CLAIM_QUERY, model=MODEL_A, dimensions=1536
            )

    async def test_rejects_empty_response(self):
        provider = GoogleGeminiEmbeddingProvider(api_key="stub-key", logger=logger)
        provider.client = SimpleNamespace(embeddings=_StubEmptyResponseApi())

        with pytest.raises(EmbeddingProviderError):
            await provider.embed(
                AKZ_NORMALIZED, purpose=PURPOSE_CLAIM_QUERY, model=MODEL_A, dimensions=1536
            )


class _StubEmptyResponseApi:
    async def create(self, **kwargs):
        del kwargs
        return SimpleNamespace(data=[])


# ---------------------------------------------------------------------------
# Atomic handoff wiring in ClaimExtractionService._persist_success
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestClaimSuccessDefersEmbedClaim:
    async def test_successful_extraction_defers_one_embed_per_new_claim(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure
        from src.config_loader import EmbeddingConfig
        from src.processing.claims import ClaimExtractionService

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content=_SEVEN_SENTENCES)
        from src.repositories.claims import ClaimExtractionPolicyRepository
        from src.repositories.relevance import RelevancePolicyVersionRepository

        relevance_policy = await RelevancePolicyVersionRepository().insert(
            conn,
            edition_id=edition.id,
            version=99,
            config_hash="wire-rel",
            prompt_version="pv",
        )
        decision = await EditionRelevanceDecisionRepository().insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=relevance_policy.id,
            status="relevant",
            confidence=None,
            reason="wiring setup",
        )
        extraction_policy = await ClaimExtractionPolicyRepository().insert(
            conn,
            edition_id=edition.id,
            version=99,
            config_hash="wire-claim",
            prompt_version="pv",
        )

        class _TwoClaimProvider:
            async def chat_completion(self, **kwargs):
                del kwargs
                import json

                return json.dumps(
                    {
                        "claims": [
                            {
                                "assertion_text": "Вода пропала.",
                                "normalized_assertion": AKZ_NORMALIZED,
                            },
                            {
                                "assertion_text": "Обещали восстановить.",
                                "normalized_assertion": SHORT_CLAIM,
                            },
                        ]
                    }
                )

        service = ClaimExtractionService(
            uow=uow,
            provider=_TwoClaimProvider(),
            model="test-model",
            provider_name="fake",
            embedding_config=EmbeddingConfig(),
        )
        result = await service.extract(revision.id, edition.id, decision.id, extraction_policy.id)

        assert len(result.claims) == 2
        async with pool.connection() as observer:
            cursor = await observer.execute(
                """
                SELECT args->>'claim_id', args->>'model', args->>'dimensions'
                FROM procrastinate.procrastinate_jobs
                WHERE task_name = 'src.jobs.processing.embed_claim'
                ORDER BY id
                """
            )
            rows = await cursor.fetchall()
        assert len(rows) == 2
        assert {int(r[0]) for r in rows} == {c.id for c in result.claims}
        assert {r[1] for r in rows} == {"gemini-embedding-2"}
        assert {int(r[2]) for r in rows} == {1536}

    async def test_without_embedding_config_no_defer_happens(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure
        from src.processing.claims import ClaimExtractionService
        from src.repositories.claims import ClaimExtractionPolicyRepository
        from src.repositories.relevance import RelevancePolicyVersionRepository

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content="Простое сообщение")
        relevance_policy = await RelevancePolicyVersionRepository().insert(
            conn, edition_id=edition.id, version=98, config_hash="nowire", prompt_version="pv"
        )
        decision = await EditionRelevanceDecisionRepository().insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=relevance_policy.id,
            status="relevant",
            confidence=None,
            reason="setup",
        )
        extraction_policy = await ClaimExtractionPolicyRepository().insert(
            conn, edition_id=edition.id, version=98, config_hash="nowire", prompt_version="pv"
        )

        class _OneClaimProvider:
            async def chat_completion(self, **kwargs):
                del kwargs
                import json

                return json.dumps(
                    {"claims": [{"assertion_text": "т", "normalized_assertion": "Т"}]}
                )

        service = ClaimExtractionService(
            uow=uow,
            provider=_OneClaimProvider(),
            model="test-model",
            provider_name="fake",
        )
        result = await service.extract(revision.id, edition.id, decision.id, extraction_policy.id)

        assert len(result.claims) == 1
        async with pool.connection() as observer:
            cursor = await observer.execute(
                "SELECT count(*) FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = 'src.jobs.processing.embed_claim'"
            )
            assert (await cursor.fetchone())[0] == 0
