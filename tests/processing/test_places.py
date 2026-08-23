"""Place mentions, versioned place resolution, and lightweight entities
(Plan 3 Task 8).

Covers the Step-1 assertions:

* two Places may both carry alias «Центр» (normalized_alias is indexed,
  never globally unique);
* an explicit unresolved result (``place_id IS NULL``) is a valid completed
  outcome, and a LATER policy version can resolve the same immutable
  mention;
* at-least-once execution never duplicates canonical results per
  (mention, policy);
* deterministic alias resolution preserves the original mention text;
* claims apply defers one ``resolve_place_mention`` per new mention on the
  success transaction; zero mentions defer nothing;
* ``backfill_place_mentions`` migrates metadata mentions to rows
  idempotently and leaves metadata untouched;
* the Berdyansk city-profile importer seeds places + aliases idempotently.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import psycopg
import pytest

from src.domain.claims import NewClaim
from src.repositories.claims import (
    ClaimExtractionPolicyRepository,
    ClaimExtractionRunRepository,
    ClaimRepository,
)
from src.repositories.embeddings import PURPOSE_CLAIM_QUERY, EmbeddingRepository
from src.repositories.places import (
    PlaceRepository,
    PlaceResolutionPolicyRepository,
    PlaceResolutionRunRepository,
)
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
    RelevancePolicyVersionRepository,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
BERDYANSK_PROFILE = _REPO_ROOT / "data" / "city_profiles" / "berdyansk.yaml"

_PLACE_REPO = PlaceRepository()
_POLICY_REPO = PlaceResolutionPolicyRepository()
_RUN_REPO = PlaceResolutionRunRepository()
_CLAIM_REPO = ClaimRepository()

_RELEVANCE_POLICY_REPO = RelevancePolicyVersionRepository()
_DECISION_REPO = EditionRelevanceDecisionRepository()
_EXTRACTION_POLICY_REPO = ClaimExtractionPolicyRepository()
_EXTRACTION_RUN_REPO = ClaimExtractionRunRepository()
_EMBED_REPO = EmbeddingRepository()

_T0 = dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.timezone.utc)

_COUNTER = {"n": 0}


def _next_n() -> int:
    _COUNTER["n"] += 1
    return _COUNTER["n"]


# ---------------------------------------------------------------------------
# normalize_place_text: documented normalization contract
# ---------------------------------------------------------------------------


class TestNormalizePlaceText:
    def test_lowercases_folds_cyrillic_and_collapses_whitespace(self):
        from src.processing.places import normalize_place_text

        assert normalize_place_text("  Приморская УЛИЦА ") == "приморская улица"
        assert normalize_place_text("ПРИМОРСКАЯ") == "приморская"  # explicit Cyrillic upper map

    def test_punctuation_becomes_space_so_numbered_areas_match(self):
        from src.processing.places import normalize_place_text

        # «Округ №32», «Округ 32» and «округ, 32!» must collide on one key.
        assert normalize_place_text("Округ №32") == "округ 32"
        assert normalize_place_text("Округ 32") == "округ 32"
        assert normalize_place_text("округ, 32!") == "округ 32"

    def test_alias_variants_normalize_identically(self):
        from src.processing.places import normalize_place_text

        assert normalize_place_text("Центр,") == normalize_place_text("центр")
        assert normalize_place_text("Бердянськ") == "бердянськ"


# ---------------------------------------------------------------------------
# Shared setup helpers
# ---------------------------------------------------------------------------


async def _make_claim(
    conn: psycopg.AsyncConnection,
    edition_id: int,
    source_item_revision_id: int,
    *,
    place_mentions: tuple[str, ...] = (),
    entities: tuple[str, ...] = (),
):
    """Spec §15 chain with metadata mentions exactly like legacy T4 rows."""
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
    run, _created = await _EXTRACTION_RUN_REPO.get_or_create_run(
        conn,
        source_item_revision_id=source_item_revision_id,
        edition_id=edition_id,
        extraction_policy_id=extraction_policy.id,
        relevance_decision_id=decision.id,
    )
    assert await _EXTRACTION_RUN_REPO.mark_succeeded(conn, run.id, completed_at=_T0)
    assertion = f"Утверждение {n}: вода на улице Приморской в АКЗ."
    metadata: dict = {}
    if place_mentions:
        metadata["place_mentions"] = list(place_mentions)
    if entities:
        metadata["entities"] = list(entities)
    claims = await _CLAIM_REPO.insert_claims(
        conn,
        run=run,
        claims=[
            NewClaim(
                assertion_text=assertion,
                normalized_assertion=assertion.lower(),
                metadata=metadata,
            )
        ],
    )
    return claims[0]


async def _seed_claim_embedding(uow, claim_id: int) -> int:
    async with uow.transaction() as db:
        embedding_id = await _EMBED_REPO.insert_claim_embedding(
            db,
            claim_id=claim_id,
            embedding=[0.5, 0.5],
            model="test-embedding-a",
            dimensions=2,
            purpose=PURPOSE_CLAIM_QUERY,
            content_hash=f"h-claim-{claim_id}",
        )
    assert embedding_id is not None
    return embedding_id


async def _create_place_with_aliases(
    conn: psycopg.AsyncConnection,
    *,
    canonical_name: str,
    aliases: tuple[str, ...] = (),
    parent_place_id: int | None = None,
    kind: str | None = None,
):
    place = await _PLACE_REPO.insert_place(
        conn, canonical_name=canonical_name, kind=kind, parent_place_id=parent_place_id
    )
    for alias in aliases:
        await _PLACE_REPO.insert_alias(conn, place_id=place.id, alias=alias)
    return place


async def _mention(conn: psycopg.AsyncConnection, claim_id: int, original_text: str):
    mention, _created = await _PLACE_REPO.create_mention(
        conn, claim_id=claim_id, original_text=original_text
    )
    return mention


async def _insert_result(
    uow,
    *,
    mention_id: int,
    edition_id: int,
    policy_id: int,
    place_id: int | None,
    status: str = "resolved",
):
    """One complete resolution outcome (run + result row).

    A duplicate execution re-reads the canonical winner instead of writing
    a second row — exactly the at-least-once convergence under test."""
    async with uow.transaction() as db:
        run = await _RUN_REPO.insert_running(
            db, mention_id=mention_id, edition_id=edition_id, policy_id=policy_id
        )
        result, _inserted = await _PLACE_REPO.insert_resolution_result(
            db,
            run_id=run.id,
            mention_id=mention_id,
            policy_id=policy_id,
            place_id=place_id,
            status=status,
            confidence=None if place_id is None else 0.9,
            reason="test",
        )
        assert await _RUN_REPO.mark_succeeded(db, run.id, completed_at=_T0)
    return result


# ---------------------------------------------------------------------------
# Alias semantics: indexed but NOT globally unique
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestPlaceAliasesAreNotGloballyUnique:
    async def test_two_places_may_both_have_alias_centr(self, conn, edition):
        center_city = await _create_place_with_aliases(
            conn, canonical_name="Центр міста", aliases=("Центр", "в центре")
        )
        center_park = await _create_place_with_aliases(
            conn, canonical_name="Центральный парк", aliases=("Центр",)
        )

        # The identical normalized alias under two places must coexist.
        candidates = await _PLACE_REPO.alias_candidates(conn, normalized_alias="центр")
        assert {p.id for p in candidates} == {center_city.id, center_park.id}

    async def test_alias_insert_is_idempotent_per_place(self, conn, edition):
        place = await _create_place_with_aliases(conn, canonical_name="АКЗ")
        first, created_first = await _PLACE_REPO.insert_alias(
            conn, place_id=place.id, alias="район АКЗ"
        )
        second, created_second = await _PLACE_REPO.insert_alias(
            conn,
            place_id=place.id,
            alias="РАЙОН АКЗ!",  # same normalized form
        )

        assert created_first is True
        assert created_second is False
        assert second.id == first.id
        count = await conn.execute(
            "SELECT count(*) FROM place_aliases WHERE place_id = %s", (place.id,)
        )
        assert (await count.fetchone())[0] == 1


# ---------------------------------------------------------------------------
# Versioned results: unresolved outcomes, reruns, at-least-once convergence
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestVersionedResolutionResults:
    async def test_explicit_unresolved_is_a_completed_outcome(
        self, uow, conn, edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        policy = await _POLICY_REPO.insert(
            conn, edition_id=edition.id, version=1, config_hash="cfg-1", prompt_version="places-v1"
        )
        mention = await _mention(conn, claim.id, "какой-то неизвестный хутор")

        result = await _insert_result(
            uow,
            mention_id=mention.id,
            edition_id=edition.id,
            policy_id=policy.id,
            place_id=None,
            status="unresolved",
        )

        assert result.place_id is None
        assert result.status == "unresolved"
        canonical = await _PLACE_REPO.find_canonical_result(
            conn, mention_id=mention.id, policy_id=policy.id
        )
        assert canonical is not None and canonical.id == result.id

    async def test_at_least_once_execution_never_duplicates_canonical_results(
        self, uow, conn, edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        policy = await _POLICY_REPO.insert(
            conn, edition_id=edition.id, version=1, config_hash="cfg-1", prompt_version="places-v1"
        )
        place = await _create_place_with_aliases(conn, canonical_name="АКЗ")
        mention = await _mention(conn, claim.id, "АКЗ")

        first = await _insert_result(
            uow,
            mention_id=mention.id,
            edition_id=edition.id,
            policy_id=policy.id,
            place_id=place.id,
        )
        duplicate = await _insert_result(
            uow,
            mention_id=mention.id,
            edition_id=edition.id,
            policy_id=policy.id,
            place_id=place.id,
        )

        cursor = await conn.execute(
            """
            SELECT count(*) FROM place_resolution_results
            WHERE mention_id = %s AND policy_id = %s AND status IN ('resolved', 'unresolved')
            """,
            (mention.id, policy.id),
        )
        assert (await cursor.fetchone())[0] == 1
        assert duplicate.id == first.id

    async def test_later_policy_resolves_the_same_immutable_mention(
        self, uow, conn, edition, revision_factory
    ):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        v1 = await _POLICY_REPO.insert(
            conn, edition_id=edition.id, version=1, config_hash="cfg-1", prompt_version="places-v1"
        )
        v2 = await _POLICY_REPO.insert(
            conn, edition_id=edition.id, version=2, config_hash="cfg-2", prompt_version="places-v2"
        )
        place = await _create_place_with_aliases(conn, canonical_name="АКЗ", aliases=("АКЗ",))
        mention = await _mention(conn, claim.id, "АКЗ")

        unresolved = await _insert_result(
            uow,
            mention_id=mention.id,
            edition_id=edition.id,
            policy_id=v1.id,
            place_id=None,
            status="unresolved",
        )
        resolved = await _insert_result(
            uow, mention_id=mention.id, edition_id=edition.id, policy_id=v2.id, place_id=place.id
        )

        # The old outcome stays immutable; the new policy owns its own slot.
        still_there = await _PLACE_REPO.find_canonical_result(
            conn, mention_id=mention.id, policy_id=v1.id
        )
        assert still_there is not None and still_there.status == "unresolved"
        assert still_there.id == unresolved.id
        upgraded = await _PLACE_REPO.find_canonical_result(
            conn, mention_id=mention.id, policy_id=v2.id
        )
        assert upgraded is not None and upgraded.place_id == place.id
        assert upgraded.id == resolved.id

    async def test_run_transitions_are_guarded(self, uow, conn, edition, revision_factory):
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        policy = await _POLICY_REPO.insert(
            conn, edition_id=edition.id, version=1, config_hash="cfg-1", prompt_version="places-v1"
        )
        mention = await _mention(conn, claim.id, "АКЗ")

        async with uow.transaction() as db:
            run = await _RUN_REPO.insert_running(
                db, mention_id=mention.id, edition_id=edition.id, policy_id=policy.id
            )
            assert await _RUN_REPO.mark_failed(db, run.id, error_kind="boom", completed_at=_T0)
            # A failed run can never flip again; only 'running' transitions.
            assert not await _RUN_REPO.mark_succeeded(db, run.id, completed_at=_T0)


# ---------------------------------------------------------------------------
# PlaceResolutionService: deterministic alias resolution end to end
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestPlaceResolutionServiceFlow:
    async def test_alias_hit_resolves_mention_and_preserves_original_text(
        self, uow, conn, pool, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure
        from src.processing.places import PlaceResolutionPolicyService, PlaceResolutionService

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        place = await _create_place_with_aliases(
            conn, canonical_name="АКЗ", aliases=("АКЗ", "район АКЗ")
        )
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        policy = await PlaceResolutionPolicyService().ensure_current(conn, edition_id=edition.id)
        mention = await _mention(conn, claim.id, "  район АКЗ! ")
        service = PlaceResolutionService(uow=uow)

        result = await service.resolve_mention(mention.id, policy.id)

        assert result.place_id == place.id
        assert result.status == "resolved"
        row = await _PLACE_REPO.get_mention(conn, mention.id)
        assert row is not None and row.original_text == "  район АКЗ! "
        runs = await conn.execute(
            "SELECT status FROM place_resolution_runs WHERE mention_id = %s", (mention.id,)
        )
        assert all(r[0] == "succeeded" for r in await runs.fetchall())

    async def test_unknown_alias_lands_explicit_unresolved_outcome(
        self, uow, conn, pool, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure
        from src.processing.places import PlaceResolutionPolicyService, PlaceResolutionService

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        policy = await PlaceResolutionPolicyService().ensure_current(conn, edition_id=edition.id)
        mention = await _mention(conn, claim.id, "неизвестный хутор")
        service = PlaceResolutionService(uow=uow)

        result = await service.resolve_mention(mention.id, policy.id)

        # NULL place_id is a COMPLETED resolution, never a failure.
        assert result.place_id is None
        assert result.status == "unresolved"
        assert await _PLACE_REPO.find_canonical_result(
            conn, mention_id=mention.id, policy_id=policy.id
        )

    async def test_duplicate_execution_replays_without_new_rows(
        self, uow, conn, pool, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure
        from src.processing.places import PlaceResolutionPolicyService, PlaceResolutionService

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        place = await _create_place_with_aliases(conn, canonical_name="АКЗ", aliases=("АКЗ",))
        claim = await _make_claim(conn, edition.id, (await revision_factory()).id)
        policy = await PlaceResolutionPolicyService().ensure_current(conn, edition_id=edition.id)
        mention = await _mention(conn, claim.id, "АКЗ")
        service = PlaceResolutionService(uow=uow)

        first = await service.resolve_mention(mention.id, policy.id)
        replay = await service.resolve_mention(mention.id, policy.id)

        assert replay.id == first.id
        assert replay.place_id == place.id
        cursor = await conn.execute(
            "SELECT count(*) FROM place_resolution_results WHERE mention_id = %s",
            (mention.id,),
        )
        assert (await cursor.fetchone())[0] == 1
        runs = await conn.execute(
            "SELECT count(*) FROM place_resolution_runs WHERE mention_id = %s",
            (mention.id,),
        )
        assert (await runs.fetchone())[0] == 1  # replay wrote no second run


# ---------------------------------------------------------------------------
# Claims apply: materialize mentions/entities + defer resolution atomically
# ---------------------------------------------------------------------------


async def _deferred_place_jobs(pool) -> list[dict]:
    async with pool.connection() as observer:
        cursor = await observer.execute(
            """
            SELECT args FROM procrastinate.procrastinate_jobs
            WHERE task_name = 'resolve_place_mention' ORDER BY id
            """
        )
        return [dict(row[0]) for row in await cursor.fetchall()]


@pytest.mark.postgres
class TestClaimsApplyDefersPlaceResolution:
    async def test_success_txn_materializes_mentions_and_defers_one_task_per_mention(
        self, uow, pool, conn, edition, revision, production_jobs_app, monkeypatch
    ):
        import json as _json

        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure
        from src.processing.claims import ClaimExtractionService

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        relevance_policy = await _RELEVANCE_POLICY_REPO.insert(
            conn, edition_id=edition.id, version=1, config_hash="r1", prompt_version="rp"
        )
        decision = await _DECISION_REPO.insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=relevance_policy.id,
            status="relevant",
            confidence=None,
            reason="setup",
        )
        extraction_policy = await _EXTRACTION_POLICY_REPO.insert(
            conn, edition_id=edition.id, version=1, config_hash="e1", prompt_version="ep"
        )

        class _ScriptedProvider:
            async def chat_completion(self, **kwargs):
                del kwargs
                return _json.dumps(
                    {
                        "claims": [
                            {
                                "assertion_text": "Вода на АКЗ.",
                                "normalized_assertion": "На территории АКЗ возле почты появилась вода.",
                                "place_mentions": ["АКЗ", "АКЗ"],
                                "entities": ["почта"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )

        service = ClaimExtractionService(
            uow=uow,
            provider=_ScriptedProvider(),
            model="test-model",
            provider_name="fake",
            place_resolution_handoff=True,
        )

        result = await service.extract(revision.id, edition.id, decision.id, extraction_policy.id)

        claim = result.claims[0]
        mentions = await _PLACE_REPO.list_mentions_for_claim(conn, claim.id)
        # The duplicated raw text collapses onto ONE idempotent mention row.
        assert [m.original_text for m in mentions] == ["АКЗ"]
        entities = await _PLACE_REPO.list_entities_for_claim(conn, claim.id)
        assert [e.normalized_text for e in entities] == ["почта"]
        # One resolve task per NEWLY-CREATED mention, frozen to current policy.
        jobs = await _deferred_place_jobs(pool)
        assert len(jobs) == 1
        assert int(jobs[0]["mention_id"]) == mentions[0].id
        assert int(jobs[0]["policy_id"]) > 0
        policies = await _POLICY_REPO.list_for_edition(conn, edition.id)
        assert int(jobs[0]["policy_id"]) == policies[-1].id

    async def test_zero_mentions_defer_nothing(
        self, uow, pool, conn, edition, revision, production_jobs_app
    ):
        import json as _json

        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure
        from src.processing.claims import ClaimExtractionService

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        relevance_policy = await _RELEVANCE_POLICY_REPO.insert(
            conn, edition_id=edition.id, version=9, config_hash="r9", prompt_version="rp"
        )
        decision = await _DECISION_REPO.insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=relevance_policy.id,
            status="relevant",
            confidence=None,
            reason="setup",
        )
        extraction_policy = await _EXTRACTION_POLICY_REPO.insert(
            conn, edition_id=edition.id, version=9, config_hash="e9", prompt_version="ep"
        )

        class _ScriptedProvider:
            async def chat_completion(self, **kwargs):
                del kwargs
                return _json.dumps(
                    {
                        "claims": [
                            {
                                "assertion_text": "Вода дали.",
                                "normalized_assertion": "Воду дали вчера.",
                            }
                        ]
                    }
                )

        service = ClaimExtractionService(
            uow=uow,
            provider=_ScriptedProvider(),
            model="test-model",
            provider_name="fake",
            place_resolution_handoff=True,
        )

        result = await service.extract(revision.id, edition.id, decision.id, extraction_policy.id)

        assert len(result.claims) == 1
        assert await _deferred_place_jobs(pool) == []

    async def test_exploding_resolution_defer_rolls_back_whole_success(
        self, uow, pool, conn, edition, revision, production_jobs_app, monkeypatch
    ):
        import json as _json

        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure
        from src.processing.claims import ClaimExtractionService

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        relevance_policy = await _RELEVANCE_POLICY_REPO.insert(
            conn, edition_id=edition.id, version=7, config_hash="r7", prompt_version="rp"
        )
        decision = await _DECISION_REPO.insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=relevance_policy.id,
            status="relevant",
            confidence=None,
            reason="setup",
        )
        extraction_policy = await _EXTRACTION_POLICY_REPO.insert(
            conn, edition_id=edition.id, version=7, config_hash="e7", prompt_version="ep"
        )

        class _ExplodingDefer:
            def configure(self, **_kwargs):
                return self

            async def defer_async(self, **_kwargs):
                raise RuntimeError("place resolution defer exploded")

        monkeypatch.setattr(jobs_processing, "resolve_place_mention", _ExplodingDefer())

        class _ScriptedProvider:
            async def chat_completion(self, **kwargs):
                del kwargs
                return _json.dumps(
                    {
                        "claims": [
                            {
                                "assertion_text": "Вода на АКЗ.",
                                "normalized_assertion": "На АКЗ появилась вода.",
                                "place_mentions": ["АКЗ"],
                            }
                        ]
                    }
                )

        service = ClaimExtractionService(
            uow=uow,
            provider=_ScriptedProvider(),
            model="test-model",
            provider_name="fake",
            place_resolution_handoff=True,
        )

        with pytest.raises(RuntimeError, match="place resolution defer exploded"):
            await service.extract(revision.id, edition.id, decision.id, extraction_policy.id)

        # Atomic domain write: no claim, no mention, no policy pointer leak.
        async with pool.connection() as observer:
            claims_count = await observer.execute("SELECT count(*) FROM claims")
            mentions_count = await observer.execute("SELECT count(*) FROM claim_place_mentions")
            policies_count = await observer.execute(
                "SELECT count(*) FROM place_resolution_policy_versions"
            )
        assert (await claims_count.fetchone())[0] == 0
        assert (await mentions_count.fetchone())[0] == 0
        assert (await policies_count.fetchone())[0] == 0


# ---------------------------------------------------------------------------
# Backfill: metadata → rows idempotently; resolutions for gap mentions
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestBackfillPlaceMentions:
    async def test_metadata_mentions_become_rows_idempotently(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure
        from src.jobs.processing import backfill_place_mentions

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        claim = await _make_claim(
            conn,
            edition.id,
            (await revision_factory()).id,
            place_mentions=("АКЗ", "Приморская"),
            entities=("почта",),
        )
        before = await conn.execute("SELECT metadata FROM claims WHERE id = %s", (claim.id,))
        metadata_before = (await before.fetchone())[0]

        created_first = await backfill_place_mentions()
        created_again = await backfill_place_mentions()

        assert created_first == 3  # two mentions + one entity
        assert created_again == 0
        mentions = await _PLACE_REPO.list_mentions_for_claim(conn, claim.id)
        assert sorted(m.original_text for m in mentions) == ["АКЗ", "Приморская"]
        entities = await _PLACE_REPO.list_entities_for_claim(conn, claim.id)
        assert [e.normalized_text for e in entities] == ["почта"]
        after = await conn.execute("SELECT metadata FROM claims WHERE id = %s", (claim.id,))
        assert (await after.fetchone())[0] == metadata_before  # untouched

    async def test_backfill_resolutions_queues_only_gap_mentions_once(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure
        from src.jobs.processing import backfill_place_resolutions
        from src.processing.places import PlaceResolutionPolicyService

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        claim = await _make_claim(
            conn, edition.id, (await revision_factory()).id, place_mentions=("АКЗ",)
        )
        covered, gap = (
            await _mention(conn, claim.id, "АКЗ"),
            await _mention(conn, claim.id, "Приморская"),
        )
        policy = await PlaceResolutionPolicyService().ensure_current(conn, edition_id=edition.id)
        place = await _create_place_with_aliases(conn, canonical_name="АКЗ", aliases=("АКЗ",))
        await _insert_result(
            uow,
            mention_id=covered.id,
            edition_id=edition.id,
            policy_id=policy.id,
            place_id=place.id,
        )

        queued = await backfill_place_resolutions(edition.id, policy.id)

        assert queued == 1
        jobs = await _deferred_place_jobs(pool)
        assert [int(j["mention_id"]) for j in jobs] == [gap.id]
        # Idempotent once workers land: simulate the queued execution
        # completing (an explicit unresolved outcome covers too), then the
        # rerun finds no remaining debt.
        await _insert_result(
            uow,
            mention_id=gap.id,
            edition_id=edition.id,
            policy_id=policy.id,
            place_id=None,
            status="unresolved",
        )
        assert await backfill_place_resolutions(edition.id, policy.id) == 0
        assert len(await _deferred_place_jobs(pool)) == 1


# ---------------------------------------------------------------------------
# City profile importer: seed places + aliases idempotently
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestCityProfileImporter:
    async def test_import_seeds_hierarchy_aliases_and_is_idempotent(self, conn, edition):
        from src.processing.places import import_places_from_city_profile

        imported = await import_places_from_city_profile(conn, BERDYANSK_PROFILE)
        assert imported > 0

        city = await conn.execute(
            "SELECT id, kind, parent_place_id FROM places WHERE canonical_name = 'Бердянск'"
        )
        city_row = await city.fetchone()
        assert city_row is not None
        city_id, city_kind, city_parent = city_row
        assert city_kind == "city"
        assert city_parent is None

        # Settlements hang off the city.
        azovske = await conn.execute(
            "SELECT kind, parent_place_id FROM places WHERE canonical_name = 'Азовское'"
        )
        azovske_row = await azovske.fetchone()
        assert azovske_row is not None
        assert azovske_row[0] == "village"
        assert azovske_row[1] == city_id

        # Neighborhood area with its own aliases.
        akz = await conn.execute("SELECT id FROM places WHERE canonical_name = 'АКЗ'")
        akz_row = await akz.fetchone()
        assert akz_row is not None

        # Both RU and UK spellings resolve through aliases (normalized keys).
        from src.processing.places import normalize_place_text

        for alias in ("Бердянськ", "Berdyansk", "бердянск"):
            found = await _PLACE_REPO.alias_candidates(
                conn, normalized_alias=normalize_place_text(alias)
            )
            assert any(p.id == city_id for p in found), alias

        # The YAML repeats «на горе» under TWO different areas — allowed.
        gora_hits = await _PLACE_REPO.alias_candidates(
            conn, normalized_alias=normalize_place_text("на горе")
        )
        assert len(gora_hits) >= 2

        counts_before = await conn.execute(
            "SELECT (SELECT count(*) FROM places), (SELECT count(*) FROM place_aliases)"
        )
        places_before, aliases_before = await counts_before.fetchone()

        imported_again = await import_places_from_city_profile(conn, BERDYANSK_PROFILE)

        assert imported_again == 0
        counts_after = await conn.execute(
            "SELECT (SELECT count(*) FROM places), (SELECT count(*) FROM place_aliases)"
        )
        assert (await counts_after.fetchone()) == (places_before, aliases_before)
