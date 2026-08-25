"""Evidence assessment + optional verification tests (Plan 3 Task 9).

Pins the non-gating contract: immutable runs freeze the EXACT claim-set
input (sorted-id sha256), clusters/members are append-only, canonical
success is unique per (story_revision_id, policy_id, input_hash), and
verification states are descriptive soft signals with NO admission-gate
field anywhere (publication_blocking/eligible/allowed/publishable banned).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from types import SimpleNamespace

import psycopg
import pytest

from src.domain.claims import NewClaim
from src.domain.evidence import (
    CONTRADICTS,
    SUPPORTS,
    ClusterMember,
    EvidenceAssessmentRun,
    EvidenceCluster,
    ProposedCluster,
)
from src.domain.stories import NewStoryRevision
from src.repositories.claims import (
    ClaimExtractionPolicyRepository,
    ClaimExtractionRunRepository,
    ClaimRepository,
)
from src.repositories.embeddings import PURPOSE_CLAIM_QUERY, EmbeddingRepository
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
    RelevancePolicyVersionRepository,
)
from src.repositories.stories import StoryRepository

_T0 = dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.timezone.utc)
_T1 = dt.datetime(2026, 8, 22, 12, 30, tzinfo=dt.timezone.utc)
_NOW = dt.datetime.now(dt.timezone.utc)
_MODEL_A = "test-embedding-a"

STORY_REPO = StoryRepository()
_RELEVANCE_POLICY_REPO = RelevancePolicyVersionRepository()
_DECISION_REPO = EditionRelevanceDecisionRepository()
_EXTRACTION_POLICY_REPO = ClaimExtractionPolicyRepository()
_RUN_REPO = ClaimExtractionRunRepository()
_CLAIM_REPO = ClaimRepository()
_EMBED_REPO = EmbeddingRepository()

_COUNTER = {"n": 0}

ASSERTIONS = [
    "Вода ушла из скважины на Приморской, дом 14.",
    "Пересыхание скважины на Приморской оставило дома без воды.",
    "Жители Приморской сообщают об отсутствии воды вторые сутки.",
    "На Приморской вода есть, давление в норме.",
]


def _next_n() -> int:
    _COUNTER["n"] += 1
    return _COUNTER["n"]


async def _make_claim(
    conn: psycopg.AsyncConnection,
    edition_id: int,
    source_item_revision_id: int,
    *,
    assertion: str | None = None,
):
    """Spec §15 chain: relevance decision -> succeeded run -> one claim."""
    n = _next_n()
    assertion = assertion or f"Утверждение номер {n}: вода пришла на улицу Приморскую."
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
    claims = await _CLAIM_REPO.insert_claims(
        conn,
        run=run,
        claims=[NewClaim(assertion_text=assertion, normalized_assertion=assertion.lower())],
    )
    return claims[0]


def _new_revision(semantic_text: str) -> NewStoryRevision:
    return NewStoryRevision(
        current_state="developing",
        semantic_text=semantic_text,
        content_hash=hashlib.sha256(semantic_text.encode("utf-8")).hexdigest(),
        created_at=_T1,
    )


async def _seed_story(
    conn: psycopg.AsyncConnection,
    edition_id: int,
    *,
    semantic_text: str,
    lifecycle_state: str = "active",
    created_at: dt.datetime | None = None,
) -> SimpleNamespace:
    moment = created_at or _NOW
    cursor = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, created_at)
        VALUES (%s, %s, %s) RETURNING id
        """,
        (edition_id, lifecycle_state, moment),
    )
    story_id = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash, created_at
        )
        VALUES (%s, 1, 'open', %s, %s, %s) RETURNING id
        """,
        (story_id, semantic_text, f"hash-{story_id}", moment),
    )
    revision_id = (await cursor.fetchone())[0]
    await conn.execute(
        "UPDATE stories SET current_revision_id = %s WHERE id = %s", (revision_id, story_id)
    )
    return SimpleNamespace(story_id=story_id, revision_id=revision_id, semantic_text=semantic_text)


async def _attach_claim(
    conn: psycopg.AsyncConnection,
    story_id: int,
    claim_id: int,
    attached_at: dt.datetime | None = None,
) -> None:
    await STORY_REPO.attach_claim(
        conn, story_id=story_id, claim_id=claim_id, attached_at=attached_at or _T1
    )


class _DesignedCorrelator:
    """Fake correlator: returns the designed clusters and records its inputs."""

    def __init__(self, plan):
        self._plan = plan
        self.calls: list[list[int]] = []

    async def cluster(self, claims):
        self.calls.append([claim.id for claim in claims])
        return self._plan(list(claims))


def _single_cluster(claims, stances=None) -> list[ProposedCluster]:
    stances = stances or [SUPPORTS] * len(claims)
    return [
        ProposedCluster(
            label=None,
            summary=None,
            members=tuple(
                ClusterMember(claim_id=claim.id, stance=stance)
                for claim, stance in zip(claims, stances)
            ),
        )
    ]


async def _runs_rows_status(conn, run_id: int) -> str:
    cursor = await conn.execute(
        "SELECT status FROM evidence_assessment_runs WHERE id = %s", (run_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    return str(row[0])


async def _frozen_claim_ids(conn, run_id: int) -> list[int]:
    cursor = await conn.execute(
        "SELECT claim_id FROM evidence_assessment_run_claims WHERE run_id = %s ORDER BY claim_id",
        (run_id,),
    )
    return [int(row[0]) for row in await cursor.fetchall()]


def _cluster_row(
    cluster_id: int, *, supporting: int = 1, contradicting: int = 0
) -> EvidenceCluster:
    return EvidenceCluster(
        id=cluster_id,
        run_id=501,
        supersedes_cluster_id=None,
        label=None,
        summary=None,
        supporting_claims=supporting,
        contradicting_claims=contradicting,
        unique_sources=supporting,
        estimated_independent_source_groups=supporting,
        metadata={},
        created_at=_T0,
    )


def _evidence_run(run_id: int = 501) -> EvidenceAssessmentRun:
    return EvidenceAssessmentRun(
        id=run_id,
        story_id=7,
        story_revision_id=17,
        edition_id=3,
        policy_id=9,
        input_hash="hash",
        started_at=_T0,
        completed_at=_T1,
        status="succeeded",
        error_kind=None,
        metadata={},
    )


# ---------------------------------------------------------------------------
# Step 1: failing tests
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestEvidenceAssessmentFlow:
    async def test_similar_claims_remain_three_claims_but_cluster_into_one_proposition(
        self, conn, uow, edition, revision_factory
    ):
        from src.processing.evidence import EvidenceService
        from src.repositories.evidence import EvidenceAssessmentPolicyRepository

        story = await _seed_story(conn, edition.id, semantic_text="Отключение воды на Приморской.")
        claims = [
            await _make_claim(
                conn, edition.id, (await revision_factory()).id, assertion=ASSERTIONS[i]
            )
            for i in range(3)
        ]
        for claim in claims:
            await _attach_claim(conn, story.story_id, claim.id)
        correlator = _DesignedCorrelator(lambda cs: _single_cluster(cs))
        policy = await EvidenceAssessmentPolicyRepository().insert(
            conn, edition_id=edition.id, version=1, config_hash="cfg", prompt_version="p1"
        )

        outcome = await EvidenceService(uow=uow, correlator=correlator).assess_story(
            story.story_id, story.revision_id, policy.id
        )

        # Three semantically similar Claims remain THREE Claims...
        cursor = await conn.execute(
            "SELECT count(*) FROM claims WHERE id = ANY(%s)", ([c.id for c in claims],)
        )
        assert (await cursor.fetchone())[0] == 3
        # ...but cluster into ONE proposition with all three members.
        assert len(outcome.clusters) == 1
        cluster = outcome.clusters[0]
        assert cluster.supporting_claims == 3
        assert cluster.run_id == outcome.run.id
        cursor = await conn.execute(
            """
            SELECT stance FROM evidence_cluster_members WHERE cluster_id = %s
            ORDER BY claim_id
            """,
            (cluster.id,),
        )
        assert [row[0] for row in await cursor.fetchall()] == ["SUPPORTS"] * 3
        # Exact frozen inputs.
        assert await _frozen_claim_ids(conn, outcome.run.id) == sorted(c.id for c in claims)
        assert await _runs_rows_status(conn, outcome.run.id) == "succeeded"
        assert correlator.calls == [[c.id for c in claims]]

    async def test_contradictory_claim_joins_with_contradicts(
        self, conn, uow, edition, revision_factory
    ):
        from src.processing.evidence import EvidenceService
        from src.repositories.evidence import EvidenceAssessmentPolicyRepository

        story = await _seed_story(conn, edition.id, semantic_text="Вода вернулась.")
        first = await _make_claim(
            conn, edition.id, (await revision_factory()).id, assertion=ASSERTIONS[0]
        )
        second = await _make_claim(
            conn, edition.id, (await revision_factory()).id, assertion=ASSERTIONS[3]
        )
        await _attach_claim(conn, story.story_id, first.id)
        await _attach_claim(conn, story.story_id, second.id)
        correlator = _DesignedCorrelator(
            lambda cs: _single_cluster(cs, stances=[SUPPORTS, CONTRADICTS])
        )
        policy = await EvidenceAssessmentPolicyRepository().insert(
            conn, edition_id=edition.id, version=1, config_hash="cfg", prompt_version="p1"
        )

        outcome = await EvidenceService(uow=uow, correlator=correlator).assess_story(
            story.story_id, story.revision_id, policy.id
        )

        cluster = outcome.clusters[0]
        assert cluster.contradicting_claims == 1
        assert cluster.supporting_claims == 1
        cursor = await conn.execute(
            """
            SELECT m.stance FROM evidence_cluster_members m
            JOIN claims c ON c.id = m.claim_id
            WHERE m.cluster_id = %s AND c.id = %s
            """,
            (cluster.id, second.id),
        )
        assert (await cursor.fetchone())[0] == "CONTRADICTS"

    async def test_new_claim_changes_input_hash_and_starts_new_run_leaving_old_intact(
        self, conn, uow, edition, revision_factory
    ):
        from src.processing.evidence import EvidenceService
        from src.repositories.evidence import EvidenceAssessmentPolicyRepository

        story = await _seed_story(conn, edition.id, semantic_text="Развитие истории.")
        first = await _make_claim(
            conn, edition.id, (await revision_factory()).id, assertion=ASSERTIONS[0]
        )
        await _attach_claim(conn, story.story_id, first.id)
        policy = await EvidenceAssessmentPolicyRepository().insert(
            conn, edition_id=edition.id, version=1, config_hash="cfg", prompt_version="p1"
        )
        correlator = _DesignedCorrelator(lambda cs: _single_cluster(cs))
        service = EvidenceService(uow=uow, correlator=correlator)

        first_outcome = await service.assess_story(story.story_id, story.revision_id, policy.id)

        second = await _make_claim(
            conn, edition.id, (await revision_factory()).id, assertion=ASSERTIONS[2]
        )
        await _attach_claim(conn, story.story_id, second.id)
        second_outcome = await service.assess_story(story.story_id, story.revision_id, policy.id)

        assert second_outcome.replayed is False
        assert second_outcome.run.id != first_outcome.run.id
        cursor = await conn.execute(
            """
            SELECT input_hash FROM evidence_assessment_runs
            WHERE id IN (%s, %s) ORDER BY id
            """,
            (first_outcome.run.id, second_outcome.run.id),
        )
        hashes = [row[0] for row in await cursor.fetchall()]
        assert hashes[0] != hashes[1]
        # Old cluster set untouched; new assessment produced its own clusters.
        assert [c.id for c in first_outcome.clusters] != [c.id for c in second_outcome.clusters]
        cursor = await conn.execute(
            "SELECT count(*) FROM evidence_clusters WHERE run_id = %s",
            (first_outcome.run.id,),
        )
        assert (await cursor.fetchone())[0] == len(first_outcome.clusters)
        assert await _frozen_claim_ids(conn, first_outcome.run.id) == [first.id]
        assert await _frozen_claim_ids(conn, second_outcome.run.id) == sorted([first.id, second.id])

    async def test_identical_rerun_reuses_canonical_successful_run(
        self, conn, uow, edition, revision_factory
    ):
        from src.processing.evidence import EvidenceService
        from src.repositories.evidence import EvidenceAssessmentPolicyRepository

        story = await _seed_story(conn, edition.id, semantic_text="Одна история.")
        claim = await _make_claim(
            conn, edition.id, (await revision_factory()).id, assertion=ASSERTIONS[0]
        )
        await _attach_claim(conn, story.story_id, claim.id)
        policy = await EvidenceAssessmentPolicyRepository().insert(
            conn, edition_id=edition.id, version=1, config_hash="cfg", prompt_version="p1"
        )
        correlator = _DesignedCorrelator(lambda cs: _single_cluster(cs))
        service = EvidenceService(uow=uow, correlator=correlator)

        first = await service.assess_story(story.story_id, story.revision_id, policy.id)
        second = await service.assess_story(story.story_id, story.revision_id, policy.id)

        assert second.replayed is True
        assert second.run.id == first.run.id
        assert [c.id for c in second.clusters] == [c.id for c in first.clusters]
        cursor = await conn.execute(
            """
            SELECT count(*) FROM evidence_assessment_runs
            WHERE story_revision_id = %s AND policy_id = %s AND status = 'succeeded'
            """,
            (story.revision_id, policy.id),
        )
        assert (await cursor.fetchone())[0] == 1


@pytest.mark.postgres
class TestEvidencePolicyIdentity:
    async def test_ensure_current_is_identity_only_without_editions_pointer(self, conn, edition):
        from src.repositories.evidence import (
            EvidenceAssessmentPolicyRepository,
            EvidencePolicyService,
        )

        service = EvidencePolicyService()
        first = await service.ensure_current(conn, edition_id=edition.id)
        again = await service.ensure_current(conn, edition_id=edition.id)
        assert first.id == again.id
        policies = await EvidenceAssessmentPolicyRepository().list_for_edition(conn, edition.id)
        assert [policy.id for policy in policies] == [first.id]

        evolved = await service.ensure_current(
            conn, edition_id=edition.id, config_hash="changed-config"
        )
        assert evolved.version > first.version


# ---------------------------------------------------------------------------
# Optional verification: soft states, never an admission gate
# ---------------------------------------------------------------------------


class TestVerificationHeuristicMapping:
    async def test_single_claim_cluster_is_reported_low_with_no_gate_field(self):
        from src.processing.verification import VerificationService

        verification_service = VerificationService()

        assessments = await verification_service.assess(
            run=_evidence_run(),
            clusters=[_cluster_row(9001)],
            policy_id=1,
        )

        assert assessments[0].state == "reported"
        assert assessments[0].risk_level == "low"
        assert not hasattr(assessments[0], "publication_blocking")
        field_names = {field.name for field in type(assessments[0]).__dataclass_fields__.values()}
        assert not field_names & {"eligible", "allowed", "publishable"}

    async def test_mapping_covers_contradictions_corroboration_and_uncertainty(self):
        from src.processing.verification import VerificationService

        service = VerificationService()
        run = _evidence_run()

        disputed = await service.assess(
            run=run, clusters=[_cluster_row(1, contradicting=2)], policy_id=1
        )
        corroborated = await service.assess(
            run=run, clusters=[_cluster_row(2, supporting=3)], policy_id=1
        )
        uncertain = await service.assess(
            run=run,
            policy_id=1,
            clusters=[
                EvidenceCluster(
                    id=3,
                    run_id=501,
                    supersedes_cluster_id=None,
                    label=None,
                    summary=None,
                    supporting_claims=0,
                    contradicting_claims=0,
                    unique_sources=1,
                    estimated_independent_source_groups=1,
                    metadata={},
                    created_at=_T0,
                )
            ],
        )

        assert disputed[0].state == "disputed"
        assert disputed[0].risk_level == "medium"
        assert corroborated[0].state == "corroborated"
        assert corroborated[0].risk_level == "low"
        assert uncertain[0].state == "reported"
        assert uncertain[0].risk_level is None


@pytest.mark.postgres
class TestVerificationPersistence:
    async def test_verify_run_persists_soft_assessments_immutably(
        self, conn, uow, pool, edition, revision_factory, production_jobs_app
    ):
        from src.jobs.processing import VERIFY_EVIDENCE_TASK_NAME
        from src.processing.evidence import EvidenceService
        from src.processing.verification import VerificationService
        from src.repositories.evidence import EvidenceAssessmentPolicyRepository

        story = await _seed_story(conn, edition.id, semantic_text="Проверка верификации.")
        claim = await _make_claim(
            conn, edition.id, (await revision_factory()).id, assertion=ASSERTIONS[0]
        )
        await _attach_claim(conn, story.story_id, claim.id)
        policy = await EvidenceAssessmentPolicyRepository().insert(
            conn, edition_id=edition.id, version=1, config_hash="cfg", prompt_version="p1"
        )
        outcome = await EvidenceService(
            uow=uow, correlator=_DesignedCorrelator(lambda cs: _single_cluster(cs))
        ).assess_story(story.story_id, story.revision_id, policy.id)

        service = VerificationService(uow=uow)
        result = await service.verify_run(outcome.run.id)
        assert result.degraded is None
        cursor = await conn.execute(
            """
            SELECT state, risk_level, cluster_id FROM verification_assessments
            WHERE evidence_assessment_run_id = %s
            """,
            (outcome.run.id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == len(outcome.clusters)
        assert rows[0][0] == "reported"
        assert rows[0][1] == "low"
        assert rows[0][2] == outcome.clusters[0].id
        # Replay converges: no duplicate rows on rerun.
        again = await service.verify_run(outcome.run.id)
        assert again.persisted_count == 0
        cursor = await conn.execute(
            "SELECT count(*) FROM verification_assessments WHERE evidence_assessment_run_id = %s",
            (outcome.run.id,),
        )
        assert (await cursor.fetchone())[0] == len(outcome.clusters)
        # The success transaction deferred verification atomically.
        async with pool.connection() as observer:
            jobs_cursor = await observer.execute(
                "SELECT count(*) FROM procrastinate.procrastinate_jobs WHERE task_name = %s",
                (VERIFY_EVIDENCE_TASK_NAME,),
            )
            assert (await jobs_cursor.fetchone())[0] == 1

    async def test_verification_outage_records_nothing_and_fails_open(
        self, conn, uow, edition, revision_factory
    ):
        from src.processing.evidence import EvidenceService
        from src.processing.verification import VerificationService
        from src.repositories.evidence import EvidenceAssessmentPolicyRepository

        story = await _seed_story(conn, edition.id, semantic_text="Сбой верификатора.")
        claim = await _make_claim(
            conn, edition.id, (await revision_factory()).id, assertion=ASSERTIONS[0]
        )
        await _attach_claim(conn, story.story_id, claim.id)
        policy = await EvidenceAssessmentPolicyRepository().insert(
            conn, edition_id=edition.id, version=1, config_hash="cfg", prompt_version="p1"
        )
        outcome = await EvidenceService(
            uow=uow, correlator=_DesignedCorrelator(lambda cs: _single_cluster(cs))
        ).assess_story(story.story_id, story.revision_id, policy.id)

        class _ExplodingAssessments:
            async def count_for_run(self, conn, *, run_id: int, policy_id: int) -> int:
                del conn, run_id, policy_id
                return 0

            async def insert_assessments(self, conn, **_kwargs):
                raise RuntimeError("verification provider exploded")

        service = VerificationService(uow=uow, assessments=_ExplodingAssessments())
        result = await service.verify_run(outcome.run.id)

        assert result.degraded == "verification_unavailable"
        cursor = await conn.execute(
            "SELECT count(*) FROM verification_assessments WHERE evidence_assessment_run_id = %s",
            (outcome.run.id,),
        )
        assert (await cursor.fetchone())[0] == 0
        # Advisory absence NEVER touches the evidence run itself.
        assert await _runs_rows_status(conn, outcome.run.id) == "succeeded"


# ---------------------------------------------------------------------------
# Story Matching completion extension
# ---------------------------------------------------------------------------

MATCH_TASK = "src.jobs.processing.match_claim"
ASSESS_EVIDENCE_TASK = "assess_evidence"


def _service(uow, matcher) -> object:
    from src.processing.story_matching import StoryMatchingService

    return StoryMatchingService(uow=uow, matcher=matcher)


class _FixedMatcher:
    def __init__(self, payload: dict):
        self.payload = payload

    async def choose(self, claim, views, *, edition_name=None):
        del edition_name
        from src.processing.story_matching import MatchProposal

        return MatchProposal.from_dict(self.payload)


async def _seed_claim_embedding(uow, claim_id: int) -> int:
    async with uow.transaction() as db:
        embedding_id = await _EMBED_REPO.insert_claim_embedding(
            db,
            claim_id=claim_id,
            embedding=[0.5, 0.5],
            model=_MODEL_A,
            dimensions=2,
            purpose=PURPOSE_CLAIM_QUERY,
            content_hash=f"h-claim-{claim_id}",
        )
    assert embedding_id is not None
    return embedding_id


async def _insert_matching_policy(conn, edition_id: int):
    from src.repositories.story_candidates import StoryMatchingPolicyVersionRepository

    return await StoryMatchingPolicyVersionRepository().insert(
        conn,
        edition_id=edition_id,
        version=1,
        config_hash="matching-cfg",
        prompt_version="v1",
        vector_limit=20,
        lexical_limit=10,
        state_fallback_limit=20,
        total_candidate_limit=40,
        resolved_lookback_days=30,
        embedding_model=_MODEL_A,
        embedding_dimensions=2,
    )


async def _deferred_jobs(pool, task_name: str) -> list[dict]:
    async with pool.connection() as observer:
        cursor = await observer.execute(
            "SELECT args FROM procrastinate.procrastinate_jobs WHERE task_name = %s ORDER BY id",
            (task_name,),
        )
        return [{"args": dict(row[0])} for row in await cursor.fetchall()]


@pytest.mark.postgres
class TestMatchingCompletionDefersEvidence:
    async def test_new_story_apply_defers_assess_evidence_atomically(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src.repositories.evidence import EvidenceAssessmentPolicyRepository

        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_matching_policy(conn, edition.id)
        matcher = _FixedMatcher(
            {
                "assignment": "NEW_STORY",
                "story_update": {
                    "semantic_changed": True,
                    "current_state": "open",
                    "semantic_text": "Новая история про воду на Приморской.",
                },
            }
        )

        outcome = await _service(uow, matcher).run(claim.id, policy.id, embedding_id)

        assert outcome.story_id is not None
        jobs = await _deferred_jobs(pool, ASSESS_EVIDENCE_TASK)
        assert len(jobs) == 1
        args = jobs[0]["args"]
        assert int(args["story_id"]) == outcome.story_id
        assert int(args["story_revision_id"]) == outcome.revision.id
        policies = await EvidenceAssessmentPolicyRepository().list_for_edition(conn, edition.id)
        assert [p.id for p in policies] == [int(args["policy_id"])]

    async def test_attach_only_same_story_also_defers_assess_evidence(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        target = await _seed_story(
            conn, edition.id, semantic_text="Прорыв на Приморской оставил сектор без воды."
        )
        founding = await _make_claim(conn, edition.id, (await revision_factory()).id)
        await _attach_claim(conn, target.story_id, founding.id)
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        embedding_id = await _seed_claim_embedding(uow, claim.id)
        policy = await _insert_matching_policy(conn, edition.id)
        matcher = _FixedMatcher(
            {
                "assignment": "SAME_STORY",
                "target_story_id": target.story_id,
                "story_update": {"semantic_changed": False},
            }
        )

        outcome = await _service(uow, matcher).run(claim.id, policy.id, embedding_id)

        assert outcome.revision is None
        jobs = await _deferred_jobs(pool, ASSESS_EVIDENCE_TASK)
        assert len(jobs) == 1
        args = jobs[0]["args"]
        assert int(args["story_id"]) == target.story_id
        # Even without a new StoryRevision: the CURRENT revision is assessed.
        assert int(args["story_revision_id"]) == target.revision_id

    async def test_exploding_assess_defer_rolls_back_the_whole_apply(
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
        policy = await _insert_matching_policy(conn, edition.id)

        class _ExplodingDefer:
            def configure(self, **_kwargs):
                return self

            async def defer_async(self, **_kwargs):
                raise RuntimeError("assess evidence defer exploded")

        monkeypatch.setattr(jobs_processing, "assess_evidence", _ExplodingDefer())
        baseline = await conn.execute("SELECT count(*) FROM stories")
        baseline_count = (await baseline.fetchone())[0]

        with pytest.raises(RuntimeError, match="assess evidence defer exploded"):
            await _service(
                uow,
                _FixedMatcher(
                    {
                        "assignment": "NEW_STORY",
                        "story_update": {
                            "semantic_changed": True,
                            "current_state": "open",
                            "semantic_text": "Атомарность проверки откотом.",
                        },
                    }
                ),
            ).run(claim.id, policy.id, embedding_id)

        async with pool.connection() as observer:

            async def scalar(sql: str) -> int:
                cursor = await observer.execute(sql)
                return (await cursor.fetchone())[0]

            assert await scalar("SELECT count(*) FROM stories") == baseline_count
            assert await scalar("SELECT count(*) FROM story_claims") == 0
            assert await scalar("SELECT count(*) FROM story_match_decisions") == 0
            assert await scalar("SELECT count(*) FROM evidence_assessment_runs") == 0
            assert (
                await scalar(
                    f"SELECT count(*) FROM procrastinate.procrastinate_jobs "
                    f"WHERE task_name = '{ASSESS_EVIDENCE_TASK}'"
                )
                == 0
            )
            assert (
                await scalar("SELECT count(*) FROM story_matching_runs WHERE status = 'running'")
                == 1
            )
            assert (
                await scalar("SELECT count(*) FROM story_matching_runs WHERE status = 'succeeded'")
                == 0
            )


# ---------------------------------------------------------------------------
# Bounded backfill
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestBackfillEvidence:
    async def test_backfill_fills_exactly_the_gaps_once(
        self, uow, pool, conn, edition, second_edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure
        from src.jobs.processing import ASSESS_EVIDENCE_TASK, backfill_evidence
        from src.processing.evidence import EvidenceService
        from src.repositories.evidence import EvidencePolicyService

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        active = await _seed_story(conn, edition.id, semantic_text="Активная история.")
        reopened = await _seed_story(
            conn, edition.id, semantic_text="Переоткрытая история.", lifecycle_state="reopened"
        )
        recent_resolved = await _seed_story(
            conn,
            edition.id,
            semantic_text="Недавно закрытая история.",
            lifecycle_state="resolved",
            created_at=_NOW - dt.timedelta(days=5),
        )
        old_resolved = await _seed_story(
            conn,
            edition.id,
            semantic_text="Давно закрытая история вне окна.",
            lifecycle_state="resolved",
            created_at=_NOW - dt.timedelta(days=90),
        )
        foreign = await _seed_story(conn, second_edition.id, semantic_text="Чужой выпуск.")

        targets = {}
        for owning_edition, story in (
            (edition.id, active),
            (edition.id, reopened),
            (edition.id, recent_resolved),
            (edition.id, old_resolved),
            (second_edition.id, foreign),
        ):
            claim = await _make_claim(
                conn,
                owning_edition,
                (await revision_factory()).id,
                assertion=f"{story.semantic_text} утверждение {_next_n()}",
            )
            await _attach_claim(conn, story.story_id, claim.id)
            targets[story.story_id] = story

        policy = await EvidencePolicyService().ensure_current(conn, edition_id=edition.id)

        queued = await backfill_evidence(edition.id, policy.id)

        expected = {active.story_id, reopened.story_id, recent_resolved.story_id}
        jobs = await _deferred_jobs(pool, ASSESS_EVIDENCE_TASK)
        assert queued == 3
        assert {int(job["args"]["story_id"]) for job in jobs} == expected
        for job in jobs:
            story = targets[int(job["args"]["story_id"])]
            assert int(job["args"]["story_revision_id"]) == story.revision_id
            assert int(job["args"]["policy_id"]) == policy.id
        # Simulate the workers landing every queued assessment...
        service = EvidenceService(uow=uow)
        for job in jobs:
            await service.assess_story(
                int(job["args"]["story_id"]),
                int(job["args"]["story_revision_id"]),
                int(job["args"]["policy_id"]),
            )
        # ...then the bounded rerun finds zero debt.
        assert await backfill_evidence(edition.id, policy.id) == 0
        assert len(await _deferred_jobs(pool, ASSESS_EVIDENCE_TASK)) == 3


@pytest.mark.postgres
class TestOptionalVerificationAndPublicationIntegration:
    """Verifies that verification is truly optional and never blocks publication or candidate freezing."""

    async def test_unverified_story_revision_without_clusters_is_eligible_for_publication(
        self, conn: psycopg.AsyncConnection, edition, revision_factory
    ):
        from src.publication.repository import PublicationRepository

        now = dt.datetime.now(dt.timezone.utc)
        story = await _seed_story(
            conn,
            edition.id,
            semantic_text="Совершенно свежая непроверенная новость",
            created_at=now,
        )
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        await _attach_claim(conn, story.story_id, claim.id, attached_at=now)
        repo = PublicationRepository()

        eligible = await repo.eligible_story_revisions(
            conn,
            edition_id=edition.id,
            snapshot_at=now + dt.timedelta(minutes=1),
        )
        assert any(e["story_id"] == story.story_id for e in eligible)

    async def test_verification_failure_leaves_revision_publication_eligible(
        self, uow, pool, conn, edition, revision_factory
    ):
        from src.processing.evidence import EvidenceService
        from src.processing.verification import VerificationService
        from src.publication.repository import PublicationRepository
        from src.repositories.evidence import EvidencePolicyService

        now = dt.datetime.now(dt.timezone.utc)
        story = await _seed_story(
            conn,
            edition.id,
            semantic_text="История с упавшей верификацией",
            created_at=now,
        )
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        await _attach_claim(conn, story.story_id, claim.id, attached_at=now)

        policy = await EvidencePolicyService().ensure_current(conn, edition_id=edition.id)
        evidence_service = EvidenceService(uow=uow)
        outcome = await evidence_service.assess_story(story.story_id, story.revision_id, policy.id)
        assert outcome.run is not None

        # Simulate a failing provider for verification
        class _CrashingProvider:
            async def chat_completion(self, *args, **kwargs):
                raise RuntimeError("Verification AI provider is completely down")

        ver_service = VerificationService(uow=uow, provider=_CrashingProvider())
        result = await ver_service.verify_run(outcome.run.id)
        # Verification handled gracefully / degraded without raising exception
        assert result.degraded is not None or result.persisted_count >= 0

        # Story remains fully eligible for publication
        repo = PublicationRepository()
        eligible = await repo.eligible_story_revisions(
            conn,
            edition_id=edition.id,
            snapshot_at=now + dt.timedelta(minutes=1),
        )
        assert any(e["story_id"] == story.story_id for e in eligible)


@pytest.mark.postgres
class TestVerificationDeferLogging:
    """A failed optional-verification defer must log with the traceback attached."""

    async def test_defer_failure_logs_warning_with_traceback(
        self, uow, pool, conn, edition, revision_factory, monkeypatch, caplog
    ):
        import logging
        from unittest.mock import MagicMock

        import src.jobs.processing as jobs_processing
        from src.processing.evidence import EvidenceAssessmentService

        class _ExplodingDefer:
            def configure(self, *, connection=None, lock=None):
                return SimpleNamespace(defer_async=self._defer_async)

            async def _defer_async(self, **_kwargs):
                raise RuntimeError("procrastinate defer exploded")

        monkeypatch.setattr(jobs_processing, "verify_evidence", _ExplodingDefer())

        service = EvidenceAssessmentService(uow=uow)
        fake_conn = MagicMock()

        with caplog.at_level(logging.WARNING, logger="src.processing.evidence"):
            await service._defer_verification(fake_conn, run_id=42)

        records = [r for r in caplog.records if "could not defer verification" in r.message]
        assert len(records) == 1
        # The failure is advisory: evidence success must not be affected and the
        # operator still needs the full traceback for diagnosis.
        assert records[0].exc_info is not None
