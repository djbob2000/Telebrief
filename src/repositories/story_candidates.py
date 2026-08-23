"""Story candidate retrieval: bounded union of independent recall streams.

Task 7A scope (Plan 3): the `StoryCandidate` projection, the
`story_matching_policy_versions` persistence, and the retriever that unions

* vector — exact pgvector `<=>` cosine top-K via the existing
  ``EmbeddingRepository.find_story_candidates`` (model+dimensions filtered),
* lexical — Postgres FULL-TEXT similarity over the current StoryRevision
  ``title || summary || semantic_text``:
  ``to_tsvector('simple', ...) @@ websearch_to_tsquery('simple', <claim
  terms>)`` scored by ``ts_rank_cd``. The stream is a RECALL pool, so the
  claim's tokens are joined with OR (a verbatim AND of a whole Russian
  sentence would match almost nothing); ts_rank_cd still ranks documents
  sharing MORE terms higher. Tokens are sanitized to word characters before
  they reach the tsquery parser. The ``'simple'`` configuration keeps tokens
  verbatim (unstemmed) so recall never invents morphology. Because
  deployments may run C-locale databases where NEITHER the simple dictionary
  NOR ``lower()`` folds Cyrillic, both document and query text pass through
  a locale-independent ``translate()`` case fold before vectorization.
  Full-text needs NO extension: trigram similarity was considered and
  rejected because ``pg_trgm`` must be CREATE EXTENSION'd per database while
  this stream must run on any stock deployment; at this scale no GIN index
  is needed either way,
* state-fallback — active/reopened stories REGARDLESS of age plus resolved
  stories whose last activity (revision creation moment proxy) falls inside
  the policy's ``resolved_lookback_days`` window,
* place (Task 8) — stories whose attached claims resolved to places shared
  with THIS claim's resolved mentions, including ancestor/descendant WITHIN
  relations via a recursive closure over ``places.parent_place_id``
  (location_overlap 1.0 exact / 0.5 within-heuristic),
* entity (Task 8) — stories whose current-revision document contains this
  claim's normalized entities (entity_overlap = contained fraction).

Limits are bounded context/resource controls, NOT semantic thresholds: no
signal ever drops a candidate that a stream admitted. Streams are merged with
dedup by story id preferring the exact current revision (every stream already
joins ``stories.current_revision_id``), retrieval reasons merge as a set,
best score per signal wins, and the final order is deterministic (vector
distance ascending nulls last, then lexical score descending, then most
recent activity). The merged list is truncated to ``total_candidate_limit``
at the very end so the cap can only bound matcher context size.

Repositories never commit; the caller owns transaction boundaries.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

import psycopg
from psycopg.types.json import Jsonb

from src.domain.claims import Claim
from src.domain.stories import (
    FrozenStoryCandidate,
    StoryMatchDecisionRecord,
    StoryMatchingPolicyVersion,
    StoryMatchingRun,
)
from src.repositories.embeddings import (
    PURPOSE_STORY_DOCUMENT,
    EmbeddingRepository,
)

REASON_VECTOR = "retrieved_by_vector"
REASON_LEXICAL = "retrieved_by_lexical"
REASON_STATE = "retrieved_by_state"
REASON_PLACE = "retrieved_by_place"
REASON_ENTITY = "retrieved_by_entity"

# Task 8 place/entity stream bounds. Deliberately module constants (not
# policy columns yet): they are versioned-policy candidates for a future
# matching-policy migration, and nothing treats them as thresholds.
PLACE_LIMIT = 10
ENTITY_LIMIT = 10

# location_overlap semantics: an exact resolved-place match scores 1.0; a
# WITHIN relation (ancestor or descendant via places.parent_place_id) is a
# heuristic half-signal. Both are provenance metadata, never thresholds.
LOCATION_OVERLAP_EXACT = 1.0
LOCATION_OVERLAP_WITHIN = 0.5

# C-locale databases fold neither the 'simple' text-search dictionary nor
# lower() beyond ASCII, so Cyrillic/Ukrainian case folding is spelled out
# explicitly. lower() handles Latin; translate() handles the Cyrillic maps.
_CASEFOLD_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯЄІЇҐ"
_CASEFOLD_LOWER = "abcdefghijklmnopqrstuvwxyzабвгдеёжзийклмнопрстуфхцчшщъыьэюяєіїґ"


def _casefold(expression: str) -> str:
    return f"lower(translate({expression}, '{_CASEFOLD_UPPER}', '{_CASEFOLD_LOWER}'))"


def _tsquery_or_terms(text: str) -> str:
    """Sanitize claim text into an OR-of-tokens websearch query string.

    Word characters only (Unicode-aware): tsquery operators, quotes and
    punctuation from user text never reach the parser. OR semantics keep the
    stream a recall pool; ranking still favors richer term overlap.
    """
    return " or ".join(re.findall(r"\w+", text, flags=re.UNICODE))


@dataclass(frozen=True)
class StoryCandidate:
    """One retrieval hit: mandatory revision identity plus optional signals.

    Every numeric field is provenance metadata for the saved candidate row;
    none of them is an admission threshold. ``retrieval_reasons`` records
    which streams produced the candidate using the same token names as the
    boolean columns of ``story_matching_candidates``.
    """

    story_id: int
    story_revision_id: int
    story_revision_embedding_id: int | None
    retrieval_reasons: frozenset[str]
    vector_distance: float | None
    lexical_score: float | None
    location_overlap: float | None = None
    entity_overlap: float | None = None
    time_score: float | None = None
    status_score: float | None = None


@dataclass(frozen=True)
class _Hit:
    """Mergeable accumulator for one story's current-revision candidate."""

    story_id: int
    story_revision_id: int
    story_revision_embedding_id: int | None
    reasons: frozenset[str]
    vector_distance: float | None = None
    lexical_score: float | None = None
    location_overlap: float | None = None
    entity_overlap: float | None = None
    last_activity: dt.datetime | None = None


def _better(a: float | None, b: float | None, *, lower_is_better: bool) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b) if lower_is_better else max(a, b)


def _recency_key(value: dt.datetime | None) -> float:
    return value.timestamp() if value is not None else 0.0


def _merge_hit(hits: dict[int, _Hit], incoming: _Hit) -> None:
    current = hits.get(incoming.story_id)
    if current is None:
        hits[incoming.story_id] = incoming
        return
    # Every stream joins stories.current_revision_id, so duplicates always
    # carry the SAME revision identity; keep it and fuse reasons/scores.
    hits[incoming.story_id] = _Hit(
        story_id=current.story_id,
        story_revision_id=current.story_revision_id,
        story_revision_embedding_id=(
            current.story_revision_embedding_id
            if current.story_revision_embedding_id is not None
            else incoming.story_revision_embedding_id
        ),
        reasons=current.reasons | incoming.reasons,
        vector_distance=_better(
            current.vector_distance, incoming.vector_distance, lower_is_better=True
        ),
        lexical_score=_better(current.lexical_score, incoming.lexical_score, lower_is_better=False),
        location_overlap=_better(
            current.location_overlap, incoming.location_overlap, lower_is_better=False
        ),
        entity_overlap=_better(
            current.entity_overlap, incoming.entity_overlap, lower_is_better=False
        ),
        last_activity=max(
            (x for x in (current.last_activity, incoming.last_activity) if x is not None),
            default=None,
        ),
    )


class StoryMatchingPolicyVersionRepository:
    """Persistence for immutable `story_matching_policy_versions` rows."""

    _COLUMNS = """
        id, edition_id, version, config_hash, prompt_version,
        vector_limit, lexical_limit, state_fallback_limit,
        total_candidate_limit, resolved_lookback_days,
        embedding_model, embedding_dimensions, created_at
    """

    async def insert(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        version: int,
        config_hash: str,
        prompt_version: str,
        embedding_model: str,
        embedding_dimensions: int,
        vector_limit: int,
        lexical_limit: int,
        state_fallback_limit: int,
        total_candidate_limit: int,
        resolved_lookback_days: int,
    ) -> StoryMatchingPolicyVersion:
        cursor = await conn.execute(
            f"""
            INSERT INTO story_matching_policy_versions (
                edition_id, version, config_hash, prompt_version,
                vector_limit, lexical_limit, state_fallback_limit,
                total_candidate_limit, resolved_lookback_days,
                embedding_model, embedding_dimensions
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {self._COLUMNS}
            """,  # noqa: S608 — column list is a class constant; values are bound params
            (
                edition_id,
                version,
                config_hash,
                prompt_version,
                vector_limit,
                lexical_limit,
                state_fallback_limit,
                total_candidate_limit,
                resolved_lookback_days,
                embedding_model,
                embedding_dimensions,
            ),
        )
        return StoryMatchingPolicyVersion.from_row(await cursor.fetchone())

    async def get(
        self, conn: psycopg.AsyncConnection, policy_id: int
    ) -> StoryMatchingPolicyVersion | None:
        cursor = await conn.execute(
            f"""
            SELECT {self._COLUMNS}
            FROM story_matching_policy_versions WHERE id = %s
            """,  # noqa: S608 — column list is a class constant; values are bound params
            (policy_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else StoryMatchingPolicyVersion.from_row(row)

    async def list_for_edition(
        self, conn: psycopg.AsyncConnection, edition_id: int
    ) -> list[StoryMatchingPolicyVersion]:
        cursor = await conn.execute(
            f"""
            SELECT {self._COLUMNS}
            FROM story_matching_policy_versions WHERE edition_id = %s
            ORDER BY version
            """,  # noqa: S608 — column list is a class constant; values are bound params
            (edition_id,),
        )
        rows = await cursor.fetchall()
        return [StoryMatchingPolicyVersion.from_row(row) for row in rows]


class StoryCandidateRetriever:
    """Bounded multi-stream recall over one edition's eligible stories."""

    def __init__(self, embeddings: EmbeddingRepository | None = None) -> None:
        self._embeddings = embeddings or EmbeddingRepository()

    async def retrieve(
        self,
        conn: psycopg.AsyncConnection,
        *,
        claim: Claim,
        claim_embedding: Sequence[float] | None = None,
        policy: StoryMatchingPolicyVersion,
    ) -> list[StoryCandidate]:
        hits: dict[int, _Hit] = {}

        if claim_embedding:
            for vector_row in await self._embeddings.find_story_candidates(
                conn,
                edition_id=claim.edition_id,
                query_embedding=claim_embedding,
                model=policy.embedding_model,
                dimensions=policy.embedding_dimensions,
                limit=policy.vector_limit,
            ):
                _merge_hit(
                    hits,
                    _Hit(
                        story_id=vector_row.story_id,
                        story_revision_id=vector_row.story_revision_id,
                        story_revision_embedding_id=vector_row.embedding_id,
                        reasons=frozenset({REASON_VECTOR}),
                        vector_distance=vector_row.vector_distance,
                    ),
                )

        lexical_cursor = await conn.execute(_LEXICAL_SQL, _lexical_params(claim, policy))
        for lexical_row in await lexical_cursor.fetchall():
            _merge_hit(hits, _lexical_hit(lexical_row))
        state_cursor = await conn.execute(_STATE_SQL, _state_params(claim, policy))
        for state_row in await state_cursor.fetchall():
            _merge_hit(hits, _state_hit(state_row))

        # Task 8 recall streams: resolved claim places (with WITHIN hierarchy)
        # and lightweight normalized entities. Independent additions to the
        # same frozen union; soft signals only, never SAME_STORY evidence.
        place_cursor = await conn.execute(_PLACE_SQL, _place_params(claim, policy))
        for place_row in await place_cursor.fetchall():
            _merge_hit(hits, _place_hit(place_row))
        entity_cursor = await conn.execute(_ENTITY_SQL, _entity_params(claim, policy))
        for entity_row in await entity_cursor.fetchall():
            _merge_hit(hits, _entity_hit(entity_row))

        await self._fill_last_activity(conn, hits)

        ranked = sorted(
            hits.values(),
            key=lambda h: (
                h.vector_distance is None,
                h.vector_distance or 0.0,
                -(h.lexical_score or 0.0),
                -_recency_key(h.last_activity),
                -h.story_revision_id,
            ),
        )
        return [
            StoryCandidate(
                story_id=hit.story_id,
                story_revision_id=hit.story_revision_id,
                story_revision_embedding_id=hit.story_revision_embedding_id,
                retrieval_reasons=frozenset(hit.reasons),
                vector_distance=hit.vector_distance,
                lexical_score=hit.lexical_score,
                location_overlap=hit.location_overlap,
                entity_overlap=hit.entity_overlap,
            )
            for hit in ranked[: policy.total_candidate_limit]
        ]

    async def _fill_last_activity(
        self, conn: psycopg.AsyncConnection, hits: dict[int, _Hit]
    ) -> None:
        """Batch-load revision creation moments for the deterministic recency
        tiebreak (the vector stream does not return them itself)."""
        missing = [hit.story_revision_id for hit in hits.values() if hit.last_activity is None]
        if not missing:
            return
        cursor = await conn.execute(
            "SELECT id, created_at FROM story_revisions WHERE id = ANY(%s)",
            (missing,),
        )
        created = {int(row[0]): row[1] for row in await cursor.fetchall()}
        for story_id, hit in list(hits.items()):
            if hit.last_activity is None:
                hits[story_id] = replace(hit, last_activity=created.get(hit.story_revision_id))


_EMBEDDING_ID_SUBQUERY = f"""
    SELECT e.id FROM story_revision_embeddings e
    WHERE e.story_revision_id = sr.id AND e.model = %s
      AND e.dimensions = %s AND e.purpose = '{PURPOSE_STORY_DOCUMENT}'
    ORDER BY e.id LIMIT 1
"""  # noqa: S608 — static template; the only runtime values are %s-bound

_STORY_DOCUMENT_TEXT = (
    "coalesce(sr.title, '') || ' ' || coalesce(sr.summary, '') || ' ' || sr.semantic_text"
)
_FOLDED_DOCUMENT = _casefold(_STORY_DOCUMENT_TEXT)

_LEXICAL_SQL = f"""
SELECT s.id AS story_id, sr.id AS revision_id, ({_EMBEDDING_ID_SUBQUERY}) AS embedding_id,
       ts_rank_cd(
           to_tsvector('simple', {_FOLDED_DOCUMENT}),
           websearch_to_tsquery('simple', {_casefold("%s")})
       ) AS lexical_score,
       sr.created_at AS last_activity
FROM stories s
JOIN story_revisions sr ON sr.id = s.current_revision_id
WHERE s.edition_id = %s
  AND s.lifecycle_state IN ('active', 'reopened', 'resolved')
  AND to_tsvector('simple', {_FOLDED_DOCUMENT})
      @@ websearch_to_tsquery('simple', {_casefold("%s")})
ORDER BY lexical_score DESC, sr.created_at DESC, sr.id DESC
LIMIT %s
"""  # noqa: S608 — static template; the only runtime values are %s-bound

_STATE_SQL = f"""
SELECT s.id AS story_id, sr.id AS revision_id, ({_EMBEDDING_ID_SUBQUERY}) AS embedding_id,
       sr.created_at AS last_activity
FROM stories s
JOIN story_revisions sr ON sr.id = s.current_revision_id
WHERE s.edition_id = %s
  AND (
      s.lifecycle_state IN ('active', 'reopened')
      OR (s.lifecycle_state = 'resolved'
          AND sr.created_at >= now() - make_interval(days => %s))
  )
ORDER BY sr.created_at DESC, sr.id DESC
LIMIT %s
"""  # noqa: S608 — static template; the only runtime values are %s-bound

# Place stream: the recursive closure over THIS claim's resolved places
# (hops=0 exact, ancestors AND descendants via places.parent_place_id)
# joined to stories through their attached claims' resolved results.
# Ancestors and descendants use separate recursive CTEs because Postgres
# permits exactly one recursive term each; MIN(hops) keeps the best signal
# per place so any direct hit stays an exact 1.0.
_PLACE_SQL = f"""
WITH RECURSIVE seed AS (
    SELECT DISTINCT r.place_id AS place_id
    FROM claim_place_mentions m
    JOIN place_resolution_results r ON r.mention_id = m.id
    WHERE m.claim_id = %s AND r.status = 'resolved' AND r.place_id IS NOT NULL
),
ancestors(place_id, hops) AS (
    SELECT place_id, 0 FROM seed
    UNION
    SELECT p.parent_place_id, a.hops + 1
    FROM ancestors a JOIN places p ON p.id = a.place_id
    WHERE p.parent_place_id IS NOT NULL
),
descendants(place_id, hops) AS (
    SELECT place_id, 0 FROM seed
    UNION
    SELECT child.id, d.hops + 1
    FROM descendants d JOIN places child ON child.parent_place_id = d.place_id
),
closure(place_id, hops) AS (
    SELECT place_id, MIN(hops) FROM (
        SELECT place_id, hops FROM ancestors
        UNION ALL
        SELECT place_id, hops FROM descendants
    ) both_directions
    GROUP BY place_id
),
ranked AS (
    SELECT s.id AS story_id,
           sr.id AS revision_id,
           ({_EMBEDDING_ID_SUBQUERY}) AS embedding_id,
           CASE WHEN bool_or(c.hops = 0)
                THEN {LOCATION_OVERLAP_EXACT} ELSE {LOCATION_OVERLAP_WITHIN} END AS location_overlap,
           MAX(sr.created_at) AS last_activity
    FROM closure c
    JOIN place_resolution_results r
      ON r.place_id = c.place_id AND r.status = 'resolved'
    JOIN claim_place_mentions m ON m.id = r.mention_id
    JOIN story_claims sc ON sc.claim_id = m.claim_id
    JOIN stories s ON s.id = sc.story_id AND s.edition_id = %s
    JOIN story_revisions sr ON sr.id = s.current_revision_id
    WHERE s.lifecycle_state IN ('active', 'reopened', 'resolved')
    GROUP BY s.id, sr.id, embedding_id
    ORDER BY location_overlap DESC, last_activity DESC, sr.id DESC
    LIMIT %s
)
SELECT story_id, revision_id, embedding_id, location_overlap, last_activity FROM ranked
"""  # noqa: S608 — static template; the only runtime values are %s-bound

# Entity stream: fraction of the claim's normalized entities whose
# whitespace-stripped folded form occurs in the current revision document.
# Both sides fold case explicitly so C-locale databases still match Cyrillic.
_ENTITY_DOCUMENT = (
    "regexp_replace("
    "coalesce(sr.title, '') || ' ' || coalesce(sr.summary, '') || ' ' || sr.semantic_text,"
    " '\\s+', '', 'g')"
)

_ENTITY_SQL = f"""
WITH needles AS (
    SELECT DISTINCT regexp_replace(ce.normalized_text, '\\s+', '', 'g') AS needle
    FROM claim_entities ce WHERE ce.claim_id = %s
),
totals AS (SELECT count(*)::float8 AS n FROM needles)
SELECT s.id AS story_id,
       sr.id AS revision_id,
       ({_EMBEDDING_ID_SUBQUERY}) AS embedding_id,
       (SELECT count(*) FROM needles nd
        WHERE strpos({_casefold(_ENTITY_DOCUMENT)}, {_casefold("nd.needle")}) > 0)::float8 / t.n
           AS entity_overlap,
       sr.created_at AS last_activity
FROM stories s
JOIN story_revisions sr ON sr.id = s.current_revision_id
CROSS JOIN totals t
WHERE s.edition_id = %s
  AND s.lifecycle_state IN ('active', 'reopened', 'resolved')
  AND EXISTS (
      SELECT 1 FROM needles nd
      WHERE strpos({_casefold(_ENTITY_DOCUMENT)}, {_casefold("nd.needle")}) > 0
  )
ORDER BY entity_overlap DESC, last_activity DESC, sr.id DESC
LIMIT %s
"""  # noqa: S608 — static template; the only runtime values are %s-bound


def _lexical_params(claim: Claim, policy: StoryMatchingPolicyVersion) -> tuple:
    # Placeholder order follows the SQL text: the embedding-id subquery comes
    # first, then rank/where/limit parameters.
    query = _tsquery_or_terms(claim.normalized_assertion)
    return (
        policy.embedding_model,
        policy.embedding_dimensions,
        query,
        claim.edition_id,
        query,
        policy.lexical_limit,
    )


def _state_params(claim: Claim, policy: StoryMatchingPolicyVersion) -> tuple:
    return (
        policy.embedding_model,
        policy.embedding_dimensions,
        claim.edition_id,
        policy.resolved_lookback_days,
        policy.state_fallback_limit,
    )


def _lexical_hit(row: Sequence) -> _Hit:
    return _Hit(
        story_id=int(row[0]),
        story_revision_id=int(row[1]),
        story_revision_embedding_id=None if row[2] is None else int(row[2]),
        reasons=frozenset({REASON_LEXICAL}),
        lexical_score=float(row[3]),
        last_activity=row[4],
    )


def _state_hit(row: Sequence) -> _Hit:
    return _Hit(
        story_id=int(row[0]),
        story_revision_id=int(row[1]),
        story_revision_embedding_id=None if row[2] is None else int(row[2]),
        reasons=frozenset({REASON_STATE}),
        last_activity=row[3],
    )


def _place_params(claim: Claim, policy: StoryMatchingPolicyVersion) -> tuple:
    # Placeholder order follows the SQL text: claim id (seed closure), then
    # the embedding-id subquery (model/dimensions), then edition and limit.
    return (
        claim.id,
        policy.embedding_model,
        policy.embedding_dimensions,
        claim.edition_id,
        PLACE_LIMIT,
    )


def _entity_params(claim: Claim, policy: StoryMatchingPolicyVersion) -> tuple:
    return (
        claim.id,
        policy.embedding_model,
        policy.embedding_dimensions,
        claim.edition_id,
        ENTITY_LIMIT,
    )


def _place_hit(row: Sequence) -> _Hit:
    return _Hit(
        story_id=int(row[0]),
        story_revision_id=int(row[1]),
        story_revision_embedding_id=None if row[2] is None else int(row[2]),
        reasons=frozenset({REASON_PLACE}),
        location_overlap=float(row[3]),
        last_activity=row[4],
    )


def _entity_hit(row: Sequence) -> _Hit:
    return _Hit(
        story_id=int(row[0]),
        story_revision_id=int(row[1]),
        story_revision_embedding_id=None if row[2] is None else int(row[2]),
        reasons=frozenset({REASON_ENTITY}),
        entity_overlap=float(row[3]),
        last_activity=row[4],
    )


@dataclass(frozen=True)
class ClaimEmbeddingGapRow:
    """One backfill target: a compatible claim embedding with no covering
    matching run (succeeded / running / stale) for the exact policy."""

    embedding_id: int
    claim_id: int

    @classmethod
    def from_row(cls, row: Sequence) -> ClaimEmbeddingGapRow:
        return cls(embedding_id=int(row[0]), claim_id=int(row[1]))


@dataclass(frozen=True)
class LockedMatchingRun:
    """A FOR UPDATE-locked run row plus its frozen candidates.

    Holding the row lock means no other apply transaction can observe or
    mutate this run between the stale check and ``mark_succeeded``."""

    run: StoryMatchingRun
    candidates: list[FrozenStoryCandidate]

    def candidate_for(self, story_id: int) -> FrozenStoryCandidate:
        """The frozen candidate for one story; missing targets are contract
        violations of the matcher output, not silent misses."""
        for candidate in self.candidates:
            if candidate.story_id == story_id:
                return candidate
        raise KeyError(f"story {story_id} is not among the frozen candidates")


_RUN_COLUMNS = """
    id, claim_id, edition_id, policy_id, claim_embedding_id,
    started_at, completed_at, status, error_kind, metadata,
    candidates_retrieved_at, retrieval_mode
"""

_CANDIDATE_COLUMNS = """
    id, run_id, story_id, story_revision_id, story_revision_embedding_id,
    retrieved_by_vector, retrieved_by_lexical, retrieved_by_state,
    retrieved_by_place, retrieved_by_entity,
    vector_distance, lexical_score, location_overlap, entity_overlap,
    time_score, status_score, rank
"""


class StoryMatchingRunRepository:
    """Persistence for runs, their frozen candidates, decisions and relation
    proposals. The partial unique index uq_story_match_success ON
    (claim_id, policy_id) WHERE status='succeeded' is THE canonical
    duplicate guard: guarded transitions only leave 'running', so a second
    success for the key surfaces as UniqueViolation at write time."""

    async def insert_running(
        self,
        conn: psycopg.AsyncConnection,
        *,
        claim_id: int,
        edition_id: int,
        policy_id: int,
        claim_embedding_id: int | None,
        retrieval_mode: str = "knowledge_full",
    ) -> StoryMatchingRun:
        cursor = await conn.execute(
            f"""
            INSERT INTO story_matching_runs (
                claim_id, edition_id, policy_id, claim_embedding_id, status, retrieval_mode
            )
            VALUES (%s, %s, %s, %s, 'running', %s)
            RETURNING {_RUN_COLUMNS}
            """,  # noqa: S608 — column list is a module constant; values are bound params
            (claim_id, edition_id, policy_id, claim_embedding_id, retrieval_mode),
        )
        return StoryMatchingRun.from_row(await cursor.fetchone())

    async def get(self, conn: psycopg.AsyncConnection, run_id: int) -> StoryMatchingRun | None:
        cursor = await conn.execute(
            f"""
            SELECT {_RUN_COLUMNS} FROM story_matching_runs WHERE id = %s
            """,  # noqa: S608 — column list is a module constant; value is bound
            (run_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else StoryMatchingRun.from_row(row)

    async def find_succeeded(
        self, conn: psycopg.AsyncConnection, *, claim_id: int, policy_id: int
    ) -> StoryMatchingRun | None:
        cursor = await conn.execute(
            f"""
            SELECT {_RUN_COLUMNS} FROM story_matching_runs
            WHERE claim_id = %s AND policy_id = %s AND status = 'succeeded'
            ORDER BY id DESC LIMIT 1
            """,  # noqa: S608 — column list is a module constant; values are bound params
            (claim_id, policy_id),
        )
        row = await cursor.fetchone()
        return None if row is None else StoryMatchingRun.from_row(row)

    async def latest_running(
        self, conn: psycopg.AsyncConnection, *, claim_id: int, policy_id: int
    ) -> StoryMatchingRun | None:
        cursor = await conn.execute(
            f"""
            SELECT {_RUN_COLUMNS} FROM story_matching_runs
            WHERE claim_id = %s AND policy_id = %s AND status = 'running'
            ORDER BY id DESC LIMIT 1
            """,  # noqa: S608 — column list is a module constant; values are bound params
            (claim_id, policy_id),
        )
        row = await cursor.fetchone()
        return None if row is None else StoryMatchingRun.from_row(row)

    async def save_candidates(
        self,
        conn: psycopg.AsyncConnection,
        *,
        run_id: int,
        candidates: Sequence[StoryCandidate],
        retrieved_at: dt.datetime | None = None,
    ) -> None:
        """Freeze the retrieval result before any AI call: exact revision
        identity, provenance flags, soft scores, deterministic rank."""
        now = retrieved_at or dt.datetime.now(tz=dt.timezone.utc)
        for rank, candidate in enumerate(candidates, start=1):
            reasons = candidate.retrieval_reasons
            await conn.execute(
                """
                INSERT INTO story_matching_candidates (
                    run_id, story_id, story_revision_id, story_revision_embedding_id,
                    retrieved_by_vector, retrieved_by_lexical, retrieved_by_state,
                    retrieved_by_place, retrieved_by_entity,
                    vector_distance, lexical_score, location_overlap, entity_overlap,
                    time_score, status_score, rank
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    candidate.story_id,
                    candidate.story_revision_id,
                    candidate.story_revision_embedding_id,
                    "retrieved_by_vector" in reasons,
                    "retrieved_by_lexical" in reasons,
                    "retrieved_by_state" in reasons,
                    "retrieved_by_place" in reasons,
                    "retrieved_by_entity" in reasons,
                    candidate.vector_distance,
                    candidate.lexical_score,
                    candidate.location_overlap,
                    candidate.entity_overlap,
                    candidate.time_score,
                    candidate.status_score,
                    rank,
                ),
            )
        await conn.execute(
            "UPDATE story_matching_runs SET candidates_retrieved_at = %s WHERE id = %s",
            (now, run_id),
        )

    async def frozen_candidates(
        self, conn: psycopg.AsyncConnection, run_id: int
    ) -> list[FrozenStoryCandidate]:
        cursor = await conn.execute(
            f"""
            SELECT {_CANDIDATE_COLUMNS} FROM story_matching_candidates
            WHERE run_id = %s ORDER BY rank
            """,  # noqa: S608 — column list is a module constant
            (run_id,),
        )
        return [FrozenStoryCandidate.from_row(row) for row in await cursor.fetchall()]

    async def lock_with_candidates(
        self, conn: psycopg.AsyncConnection, run_id: int
    ) -> LockedMatchingRun | None:
        """SELECT ... FOR UPDATE the run row, then read its frozen candidates.

        The lock spans until the caller's transaction ends, closing the
        race between the stale check and the final succeeded transition."""
        cursor = await conn.execute(
            f"""
            SELECT {_RUN_COLUMNS} FROM story_matching_runs
            WHERE id = %s FOR UPDATE
            """,  # noqa: S608 — column list is a module constant
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return LockedMatchingRun(
            run=StoryMatchingRun.from_row(row),
            candidates=await self.frozen_candidates(conn, run_id),
        )

    async def mark_stale(
        self, conn: psycopg.AsyncConnection, run_id: int, *, completed_at: dt.datetime
    ) -> bool:
        """Guarded running -> stale; returns False when the slot moved on."""
        cursor = await conn.execute(
            """
            UPDATE story_matching_runs SET status = 'stale', completed_at = %s
            WHERE id = %s AND status = 'running'
            RETURNING id
            """,
            (completed_at, run_id),
        )
        return await cursor.fetchone() is not None

    async def mark_succeeded(
        self, conn: psycopg.AsyncConnection, run_id: int, *, completed_at: dt.datetime
    ) -> bool:
        """Guarded running -> succeeded. A concurrent winner for the same
        (claim_id, policy_id) makes this raise UniqueViolation from
        uq_story_match_success — the canonical duplicate guard."""
        cursor = await conn.execute(
            """
            UPDATE story_matching_runs SET status = 'succeeded', completed_at = %s
            WHERE id = %s AND status = 'running'
            RETURNING id
            """,
            (completed_at, run_id),
        )
        return await cursor.fetchone() is not None

    async def mark_failed(
        self,
        conn: psycopg.AsyncConnection,
        run_id: int,
        *,
        error_kind: str,
        completed_at: dt.datetime,
    ) -> bool:
        """Guarded running -> failed; NEVER demotes a succeeded winner."""
        cursor = await conn.execute(
            """
            UPDATE story_matching_runs SET status = 'failed', error_kind = %s,
                   completed_at = %s
            WHERE id = %s AND status = 'running'
            RETURNING id
            """,
            (error_kind, completed_at, run_id),
        )
        return await cursor.fetchone() is not None

    async def insert_decision(
        self,
        conn: psycopg.AsyncConnection,
        *,
        run_id: int,
        assignment: str,
        target_story_id: int | None,
        story_update: dict | None,
        confidence: float | None,
        reason: str | None,
    ) -> StoryMatchDecisionRecord:
        cursor = await conn.execute(
            """
            INSERT INTO story_match_decisions (
                run_id, assignment, target_story_id, story_update, confidence, reason
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, run_id, assignment, target_story_id, story_update,
                confidence, reason, created_at
            """,
            (run_id, assignment, target_story_id, Jsonb(story_update), confidence, reason),
        )
        return StoryMatchDecisionRecord.from_row(await cursor.fetchone())

    async def insert_relation_proposals(
        self,
        conn: psycopg.AsyncConnection,
        *,
        run_id: int,
        entries: Sequence[tuple[int, int, str]],
    ) -> int:
        """Append immutable proposals as (from_story_id, to_story_id, type)."""
        for from_story_id, to_story_id, relation_type in entries:
            await conn.execute(
                """
                INSERT INTO story_relation_proposals (
                    run_id, from_story_id, to_story_id, relation_type
                )
                VALUES (%s, %s, %s, %s)
                """,
                (run_id, from_story_id, to_story_id, relation_type),
            )
        return len(entries)

    async def list_claim_embedding_gaps(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        policy_id: int,
        model: str,
        dimensions: int,
        after_embedding_id: int | None = None,
        limit: int = 500,
    ) -> list[ClaimEmbeddingGapRow]:
        """Compatible claim embeddings still owing this exact policy a run.

        Coverage statuses are 'succeeded' (done), 'running' (in flight) and
        'stale' (a fresh task was already re-deferred); failed runs never
        cover — the debt stays visible until a run lands."""
        cursor = await conn.execute(
            """
            SELECT e.id, e.claim_id
            FROM claim_embeddings e
            JOIN claims c ON c.id = e.claim_id
            WHERE c.edition_id = %s
              AND e.id > COALESCE(%s, 0)
              AND e.model = %s AND e.dimensions = %s AND e.purpose = 'claim_query'
              AND NOT EXISTS (
                  SELECT 1 FROM story_matching_runs r
                  WHERE r.claim_id = e.claim_id AND r.policy_id = %s
                    AND r.status IN ('succeeded', 'running', 'stale')
              )
            ORDER BY e.id
            LIMIT %s
            """,
            (edition_id, after_embedding_id, model, dimensions, policy_id, limit),
        )
        return [ClaimEmbeddingGapRow.from_row(row) for row in await cursor.fetchall()]
