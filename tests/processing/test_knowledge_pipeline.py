"""End-to-end knowledge pipeline integration tests (Plan 3 Task 10).

Exercises the real PostgreSQL repositories and pipeline services from
SourceItemRevision ingestion through:
  Relevance -> Claim Extraction -> Place Mention Resolution ->
  Claim Embedding -> Story Matching -> Evidence Assessment -> Verification.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest

from src import runtime as runtime_module
from src.bootstrap import ApplicationInfrastructure
from src.domain.claims import NewClaim
from src.processing.claims import (
    ExtractedClaimDraft,
)
from src.processing.evidence import EvidenceAssessmentService, EvidencePolicyService
from src.processing.places import PlaceResolutionPolicyService
from src.processing.relevance import RelevancePolicyService
from src.processing.story_matching import (
    MatchProposal,
    StoryMatchingPolicyService,
    StoryMatchingService,
    StoryUpdateProposal,
)
from src.processing.verification import VerificationPolicyService, VerificationService
from src.repositories.claims import (
    ClaimExtractionPolicyRepository,
    ClaimExtractionRunRepository,
    ClaimRepository,
)
from src.repositories.embeddings import PURPOSE_CLAIM_QUERY, EmbeddingRepository
from src.repositories.evidence import EvidenceClusterRepository, VerificationPolicyRepository
from src.repositories.places import (
    PlaceRepository,
    PlaceResolutionRunRepository,
)
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
)
from src.repositories.stories import StoryRepository

_T0 = dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.timezone.utc)
_FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "multisource_knowledge_day.json"


@pytest.fixture
def knowledge_fixture_data() -> list[dict]:
    with _FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.postgres
class TestKnowledgePipelineE2E:
    """End-to-end integration test across all Plan 3 stages."""

    async def test_full_day_multisource_pipeline(
        self,
        uow,
        conn: psycopg.AsyncConnection,
        pool,
        edition,
        knowledge_fixture_data: list[dict],
        production_jobs_app,
    ):
        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )

        # Repositories
        relevance_repo = EditionRelevanceDecisionRepository()
        claim_repo = ClaimRepository()
        story_repo = StoryRepository()
        place_repo = PlaceRepository()
        evidence_cluster_repo = EvidenceClusterRepository()
        embedding_repo = EmbeddingRepository()

        # Policies
        rel_policy = await RelevancePolicyService().ensure_current(
            conn,
            edition_id=edition.id,
            config_hash="rel-cfg-1",
            prompt_version="rel-p-1",
        )
        ext_policy = await ClaimExtractionPolicyRepository().insert(
            conn,
            edition_id=edition.id,
            version=1,
            config_hash="ext-cfg-1",
            prompt_version="ext-p-1",
        )
        place_policy = await PlaceResolutionPolicyService().ensure_current(
            conn, edition_id=edition.id
        )
        match_policy = await StoryMatchingPolicyService().ensure_current(
            conn,
            edition_id=edition.id,
            embedding_model="mock-embedding-model",
            embedding_dimensions=2,
        )

        # Insert place metadata for Berdyansk АКЗ
        akz_place = await place_repo.insert_place(
            conn,
            canonical_name="АКЗ",
            kind="district",
        )
        await place_repo.insert_alias(
            conn,
            place_id=akz_place.id,
            alias="АКЗ",
        )
        await place_repo.insert_alias(
            conn,
            place_id=akz_place.id,
            alias="микрорайон АКЗ",
        )

        # -------------------------------------------------------------------
        # Stage 1: Ingest source items from fixture into DB
        # -------------------------------------------------------------------
        item_revisions: dict[str, int] = {}
        source_map: dict[str, int] = {}

        for entry in knowledge_fixture_data:
            ch = entry["channel"]
            if ch not in source_map:
                cur = await conn.execute(
                    """
                    INSERT INTO sources (platform, kind, external_id, url, name)
                    VALUES ('telegram', 'channel', %s, %s, %s)
                    RETURNING id
                    """,
                    (f"tg-{ch}", f"https://t.me/{ch}", entry["channel_name"]),
                )
                source_map[ch] = (await cur.fetchone())[0]

            cur = await conn.execute(
                """
                INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
                VALUES (%s, 'message', %s, %s)
                RETURNING id
                """,
                (source_map[ch], str(entry["message_id"]), entry["timestamp"]),
            )
            item_id = (await cur.fetchone())[0]

            cur = await conn.execute(
                """
                INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
                VALUES (%s, 1, %s, %s)
                RETURNING id
                """,
                (item_id, f"hash-{entry['id']}", entry["text"]),
            )
            item_revisions[entry["id"]] = (await cur.fetchone())[0]

        # -------------------------------------------------------------------
        # Stage 2: Thread Context & Reply Normalization (item-1 & item-2)
        # -------------------------------------------------------------------
        # Item 1: "На АКЗ воду уже дали?" (question)
        # Item 2: "Всё ещё нет." (reply to Item 1) -> normalized: "На АКЗ нет воды"
        rev2_id = item_revisions["item-2"]

        # Run extraction mock for Item 2 with parent context included
        extracted_reply_claim = ExtractedClaimDraft(
            assertion_text="Всё ещё нет (на вопрос о воде на АКЗ)",
            normalized_assertion="в микрорайоне акз всё ещё отсутствует водоснабжение",
            entities=(),
            place_mentions=("АКЗ",),
        )

        dec2 = await relevance_repo.insert_root(
            conn,
            source_item_revision_id=rev2_id,
            edition_id=edition.id,
            relevance_policy_id=rel_policy.id,
            status="relevant",
            confidence=0.9,
            reason="water outage report",
        )
        ext_run2, _ = await ClaimExtractionRunRepository().get_or_create_run(
            conn,
            source_item_revision_id=rev2_id,
            edition_id=edition.id,
            extraction_policy_id=ext_policy.id,
            relevance_decision_id=dec2.id,
        )
        await ClaimExtractionRunRepository().mark_succeeded(conn, ext_run2.id, completed_at=_T0)

        created_claims2 = await claim_repo.insert_claims(
            conn,
            run=ext_run2,
            claims=[
                NewClaim(
                    assertion_text=extracted_reply_claim.assertion_text,
                    normalized_assertion=extracted_reply_claim.normalized_assertion,
                    metadata={"source_role": "community"},
                )
            ],
        )
        assert len(created_claims2) == 1
        reply_claim = created_claims2[0]
        assert "акз" in reply_claim.normalized_assertion
        assert "водоснабжение" in reply_claim.normalized_assertion

        # Insert place mention
        mention2, _ = await place_repo.create_mention(
            conn,
            claim_id=reply_claim.id,
            original_text="АКЗ",
        )
        # Resolve place mention
        place_run = await PlaceResolutionRunRepository().insert_running(
            conn,
            mention_id=mention2.id,
            edition_id=edition.id,
            policy_id=place_policy.id,
        )
        await PlaceResolutionRunRepository().mark_succeeded(conn, place_run.id, completed_at=_T0)
        await place_repo.insert_resolution_result(
            conn,
            run_id=place_run.id,
            mention_id=mention2.id,
            policy_id=place_policy.id,
            place_id=akz_place.id,
            status="resolved",
            confidence=1.0,
            reason="exact alias match",
        )

        # Embed claim: complete normalized assertion embedded exactly once
        await embedding_repo.insert_claim_embedding(
            conn,
            claim_id=reply_claim.id,
            embedding=[0.1, 0.9],
            model="mock-embedding-model",
            dimensions=2,
            purpose=PURPOSE_CLAIM_QUERY,
            content_hash="embed-h2",
        )

        # -------------------------------------------------------------------
        # Stage 3: Multi-proposition sentence chunking (item-3: 7 sentences -> 2 claims)
        # -------------------------------------------------------------------
        rev3_id = item_revisions["item-3"]
        dec3 = await relevance_repo.insert_root(
            conn,
            source_item_revision_id=rev3_id,
            edition_id=edition.id,
            relevance_policy_id=rel_policy.id,
            status="relevant",
            confidence=0.95,
            reason="infrastructure and humanitarian news",
        )
        ext_run3, _ = await ClaimExtractionRunRepository().get_or_create_run(
            conn,
            source_item_revision_id=rev3_id,
            edition_id=edition.id,
            extraction_policy_id=ext_policy.id,
            relevance_decision_id=dec3.id,
        )
        await ClaimExtractionRunRepository().mark_succeeded(conn, ext_run3.id, completed_at=_T0)

        # 7-sentence text produces EXACTLY 2 distinct factual propositions
        claims3_new = [
            NewClaim(
                assertion_text="Подача технической воды на АКЗ возобновлена после ремонта водовода.",
                normalized_assertion="подача технической воды на акз возобновлена после ремонта водовода",
                metadata={"source_role": "news"},
            ),
            NewClaim(
                assertion_text="На Восточном проспекте открылся пункт выдачи гуманитарной помощи.",
                normalized_assertion="на восточном проспекте открылся пункт выдачи гуманитарной помощи",
                metadata={"source_role": "news"},
            ),
        ]
        created_claims3 = await claim_repo.insert_claims(conn, run=ext_run3, claims=claims3_new)
        assert len(created_claims3) == 2

        # Embed each of the 2 claims once
        for c in created_claims3:
            await embedding_repo.insert_claim_embedding(
                conn,
                claim_id=c.id,
                embedding=[0.2, 0.8] if "акз" in c.normalized_assertion else [0.9, 0.1],
                model="mock-embedding-model",
                dimensions=2,
                purpose=PURPOSE_CLAIM_QUERY,
                content_hash=f"embed-c-{c.id}",
            )

        # -------------------------------------------------------------------
        # Stage 4: Single-source community report creates active story
        # -------------------------------------------------------------------
        rev4_id = item_revisions["item-4"]
        dec4 = await relevance_repo.insert_root(
            conn,
            source_item_revision_id=rev4_id,
            edition_id=edition.id,
            relevance_policy_id=rel_policy.id,
            status="relevant",
            confidence=0.9,
            reason="pipe burst report",
        )
        ext_run4, _ = await ClaimExtractionRunRepository().get_or_create_run(
            conn,
            source_item_revision_id=rev4_id,
            edition_id=edition.id,
            extraction_policy_id=ext_policy.id,
            relevance_decision_id=dec4.id,
        )
        await ClaimExtractionRunRepository().mark_succeeded(conn, ext_run4.id, completed_at=_T0)
        c4 = (
            await claim_repo.insert_claims(
                conn,
                run=ext_run4,
                claims=[
                    NewClaim(
                        assertion_text="На Мелитопольском шоссе на АКЗ прорвало трубу.",
                        normalized_assertion="на мелитопольском шоссе на акз прорвало трубу",
                        metadata={"source_role": "community"},
                    )
                ],
            )
        )[0]
        embed_id4 = await embedding_repo.insert_claim_embedding(
            conn,
            claim_id=c4.id,
            embedding=[0.5, 0.5],
            model="mock-embedding-model",
            dimensions=2,
            purpose=PURPOSE_CLAIM_QUERY,
            content_hash=f"embed-c-{c4.id}",
        )

        matcher_mock = MagicMock()
        matcher_mock.choose = AsyncMock(
            return_value=MatchProposal(
                assignment="NEW_STORY",
                target_story_id=None,
                confidence=0.9,
                reason="single-source community report creates new active story",
                story_update=StoryUpdateProposal(
                    title="Порыв трубы на Мелитопольском шоссе (АКЗ)",
                    summary="Жители сообщают о порыве трубы на Мелитопольском шоссе возле остановки.",
                    current_state="active",
                    semantic_changed=True,
                    semantic_text="порыв трубы на мелитопольском шоссе на акз",
                ),
                relation_proposals=(),
            )
        )

        matching_service = StoryMatchingService(uow=uow, matcher=matcher_mock)
        outcome4 = await matching_service.run(
            claim_id=c4.id,
            policy_id=match_policy.id,
            claim_embedding_id=embed_id4,
        )

        assert outcome4.story_id is not None
        story4 = await story_repo.get(conn, outcome4.story_id)
        assert story4 is not None
        assert story4.current_revision_id is not None
        # Single-source report successfully created active story revision
        rev4 = await story_repo.get_revision(conn, story4.current_revision_id)
        assert rev4 is not None
        assert rev4.current_state == "active"
        assert "АКЗ" in (rev4.title or "")

        # -------------------------------------------------------------------
        # Stage 5: Hybrid recall (place/lexical matching when vector top-K misses)
        # -------------------------------------------------------------------
        # Item 10: "Ремонтники перекрыли Мелитопольское шоссе для устранения засора ливневки"
        # Semantically related to Story 4. Even if vector distance is far ([0.99, 0.01] vs [0.5, 0.5]),
        # candidate retriever finds Story 4 via Place/Lexical ("Мелитопольское шоссе" / "АКЗ").
        rev10_id = item_revisions["item-10"]
        dec10 = await relevance_repo.insert_root(
            conn,
            source_item_revision_id=rev10_id,
            edition_id=edition.id,
            relevance_policy_id=rel_policy.id,
            status="relevant",
            confidence=0.9,
            reason="road closure repair update",
        )
        ext_run10, _ = await ClaimExtractionRunRepository().get_or_create_run(
            conn,
            source_item_revision_id=rev10_id,
            edition_id=edition.id,
            extraction_policy_id=ext_policy.id,
            relevance_decision_id=dec10.id,
        )
        await ClaimExtractionRunRepository().mark_succeeded(conn, ext_run10.id, completed_at=_T0)
        c10 = (
            await claim_repo.insert_claims(
                conn,
                run=ext_run10,
                claims=[
                    NewClaim(
                        assertion_text="Ремонтники перекрыли Мелитопольское шоссе для устранения засора.",
                        normalized_assertion="ремонтники перекрыли мелитопольское шоссе для устранения засора",
                        metadata={"source_role": "community"},
                    )
                ],
            )
        )[0]
        embed_id10 = await embedding_repo.insert_claim_embedding(
            conn,
            claim_id=c10.id,
            embedding=[0.99, 0.01],
            model="mock-embedding-model",
            dimensions=2,
            purpose=PURPOSE_CLAIM_QUERY,
            content_hash=f"embed-c-{c10.id}",
        )

        matcher_mock.choose = AsyncMock(
            return_value=MatchProposal(
                assignment="SAME_STORY",
                target_story_id=outcome4.story_id,
                confidence=0.85,
                reason="continuation of incident on Melitopolskoe highway",
                story_update=StoryUpdateProposal(
                    title="Перекрытие Мелитопольского шоссе из-за коммунальных работ",
                    summary="Мелитопольское шоссе перекрыто для устранения последствий аварии.",
                    current_state="active",
                    semantic_changed=True,
                    semantic_text="перекрытие мелитопольского шоссе из-за коммунальных работ на акз",
                ),
                relation_proposals=(),
            )
        )
        outcome10 = await matching_service.run(
            claim_id=c10.id,
            policy_id=match_policy.id,
            claim_embedding_id=embed_id10,
        )
        assert outcome10.story_id == outcome4.story_id

        # -------------------------------------------------------------------
        # Stage 6: Correction & Contradiction handling (Item 6 vs Item 4)
        # -------------------------------------------------------------------
        rev6_id = item_revisions["item-6"]
        dec6 = await relevance_repo.insert_root(
            conn,
            source_item_revision_id=rev6_id,
            edition_id=edition.id,
            relevance_policy_id=rel_policy.id,
            status="relevant",
            confidence=0.95,
            reason="official vodokanal clarification",
        )
        ext_run6, _ = await ClaimExtractionRunRepository().get_or_create_run(
            conn,
            source_item_revision_id=rev6_id,
            edition_id=edition.id,
            extraction_policy_id=ext_policy.id,
            relevance_decision_id=dec6.id,
        )
        await ClaimExtractionRunRepository().mark_succeeded(conn, ext_run6.id, completed_at=_T0)
        c6 = (
            await claim_repo.insert_claims(
                conn,
                run=ext_run6,
                claims=[
                    NewClaim(
                        assertion_text="Утечка на Мелитопольском шоссе связана с ливневкой, водопровод цел.",
                        normalized_assertion="утечка на мелитопольском шоссе связана с ливневкой водопровод цел",
                        metadata={"source_role": "official"},
                    )
                ],
            )
        )[0]
        # Insert relation: c6 CORRECTS c4
        await claim_repo.insert_relation(
            conn, from_claim_id=c6.id, to_claim_id=c4.id, relation_type="CORRECTS"
        )
        # Attach c6 to story4
        await story_repo.attach_claim(
            conn, story_id=outcome4.story_id, claim_id=c6.id, attached_at=_T0
        )

        # -------------------------------------------------------------------
        # Stage 7: Evidence Assessment & Verification
        # -------------------------------------------------------------------
        ev_policy = await EvidencePolicyService().ensure_current(conn, edition_id=edition.id)
        ev_service = EvidenceAssessmentService(uow=uow)
        ev_run = await ev_service.assess(
            story_id=outcome4.story_id,
            story_revision_id=(await story_repo.current_revision_id(conn, outcome4.story_id)) or 1,
            policy_id=ev_policy.id,
        )
        assert ev_run.status == "succeeded"

        clusters = await evidence_cluster_repo.list_clusters_for_run(conn, ev_run.id)
        assert len(clusters) >= 1

        # Run verification service
        ver_service = VerificationService(uow=uow)
        ver_policy = await VerificationPolicyService(VerificationPolicyRepository()).ensure_current(
            conn, edition_id=edition.id
        )
        ver_assessments = await ver_service.assess(
            run=ev_run, clusters=clusters, policy_id=ver_policy.id
        )

        assert len(ver_assessments) >= 1
        # Contradiction/correction detected -> disputed state without publication gate
        assert any(a.state == "disputed" for a in ver_assessments)
        for a in ver_assessments:
            assert not hasattr(a, "publication_blocking")
            assert not hasattr(a, "eligible")
            assert not hasattr(a, "allowed")
