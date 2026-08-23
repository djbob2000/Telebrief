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
  the policy's ``resolved_lookback_days`` window.

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

from src.domain.claims import Claim
from src.domain.stories import StoryMatchingPolicyVersion
from src.repositories.embeddings import (
    PURPOSE_STORY_DOCUMENT,
    EmbeddingRepository,
)

REASON_VECTOR = "retrieved_by_vector"
REASON_LEXICAL = "retrieved_by_lexical"
REASON_STATE = "retrieved_by_state"

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
        claim_embedding: Sequence[float],
        policy: StoryMatchingPolicyVersion,
    ) -> list[StoryCandidate]:
        hits: dict[int, _Hit] = {}

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
