"""Deterministic adapter mapping frozen persistent knowledge to Story Cards and source bundles (Plan 4 Task 4)."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from src.db.uow import DatabaseUnitOfWork
from src.editorial_models import (
    EditorialAnalysis,
    PreparedBundle,
    SourceRecord,
    StoryCard,
)
from src.publication.models import PublicationGenerationAttempt
from src.publication.repository import PublicationRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrozenEditorialInput:
    """Sealed editorial analysis and source bundle ready for article generation."""

    analysis: EditorialAnalysis
    writer_bundle: PreparedBundle
    run_id: int | None = None


class GenerationAttemptObserver(Protocol):
    """Observer receiving callbacks around each writer, repair, and fallback operation."""

    async def attempt_started(
        self,
        kind: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int: ...

    async def attempt_finished(
        self,
        attempt_id: int,
        status: str,
        *,
        error_kind: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...


class NoOpGenerationAttemptObserver:
    """Default no-op observer for direct/compatibility article generation."""

    async def attempt_started(
        self,
        kind: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        return 0

    async def attempt_finished(
        self,
        attempt_id: int,
        status: str,
        *,
        error_kind: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        pass


class DatabaseGenerationAttemptObserver:
    """Production observer persisting real publication_generation_attempts rows."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        run_id: int,
        repo: PublicationRepository | None = None,
    ) -> None:
        self.uow = uow
        self.run_id = run_id
        self.repo = repo or PublicationRepository()
        self._attempt_counter = 0
        self.attempts: list[PublicationGenerationAttempt] = []
        self.last_successful_content_attempt: PublicationGenerationAttempt | None = None

    async def attempt_started(
        self,
        kind: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        self._attempt_counter += 1
        attempt_no = self._attempt_counter
        async with self.uow.transaction() as conn:
            attempt = await self.repo.insert_generation_attempt(
                conn,
                run_id=self.run_id,
                attempt_no=attempt_no,
                kind=kind,
                provider=provider,
                model=model,
                prompt_hash=prompt_hash,
                metadata=metadata,
            )
            self.attempts.append(attempt)
            return attempt.id

    async def attempt_finished(
        self,
        attempt_id: int,
        status: str,
        *,
        error_kind: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if attempt_id <= 0:
            return
        async with self.uow.transaction() as conn:
            await self.repo.update_generation_attempt(
                conn,
                attempt_id,
                status=status,
                error_kind=error_kind,
                metadata=metadata,
            )
            cursor = await conn.execute(
                """
                SELECT id, publication_run_id, attempt_no, kind, status, error_kind,
                       provider, model, prompt_hash, metadata, started_at, completed_at
                FROM publication_generation_attempts
                WHERE id = %s
                """,
                (attempt_id,),
            )
            row = await cursor.fetchone()
            if row is not None:
                updated = PublicationGenerationAttempt.from_row(row)
                if status == "succeeded":
                    self.last_successful_content_attempt = updated


class KnowledgeEditorialAdapter:
    """Builds deterministic Story Cards and source bundle from sealed publication inputs."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        repo: PublicationRepository | None = None,
    ) -> None:
        self.uow = uow
        self.repo = repo or PublicationRepository()

    async def build(self, run_id: int) -> FrozenEditorialInput:
        async with self.uow.transaction() as conn:
            run = await self.repo.lock_run(conn, run_id)
            if run is None:
                raise ValueError(f"publication run {run_id} not found")

            inputs = await self.repo.load_sealed_inputs(conn, run_id)
            if not inputs:
                raise ValueError(f"publication run {run_id} has no sealed inputs")

            cards: list[StoryCard] = []
            records: dict[str, SourceRecord] = {}

            for rank, inp in enumerate(inputs, start=1):
                # Fetch story revision
                sr_cur = await conn.execute(
                    """
                    SELECT sr.semantic_text, s.lifecycle_state
                    FROM story_revisions sr
                    JOIN stories s ON s.id = sr.story_id
                    WHERE sr.id = %s
                    """,
                    (inp.story_revision_id,),
                )
                sr_row = await sr_cur.fetchone()
                semantic_text = sr_row[0] if sr_row else "Городская новость"

                # Fetch candidate snapshot_features (which holds the frozen projected text respecting excluded platforms)
                cand_cur = await conn.execute(
                    """
                    SELECT snapshot_features
                    FROM publication_candidates
                    WHERE publication_run_id = %s AND story_id = %s AND story_revision_id = %s
                    """,
                    (run_id, inp.story_id, inp.story_revision_id),
                )
                cand_row = await cand_cur.fetchone()
                projected_text = None
                if cand_row and isinstance(cand_row[0], dict):
                    projected_text = cand_row[0].get("semantic_text")

                # Fetch claims and their sources (respecting frozen attribution snapshot from publication_input_claims)
                c_cur = await conn.execute(
                    """
                    SELECT c.id, c.assertion_text, c.normalized_assertion,
                           COALESCE(pic.source_snapshot->>'platform', s.platform) AS platform,
                           COALESCE(pic.source_name, pic.source_snapshot->>'name', s.name) AS name,
                           si.external_id, si.published_at,
                           sir.text_content, si.canonical_url,
                           COALESCE(pic.source_snapshot->>'url', s.url) AS url,
                           COALESCE(pic.source_snapshot->>'external_id', s.external_id) AS s_external_id,
                           COALESCE(pic.source_role, pic.source_snapshot->>'role', s.role) AS effective_role,
                           s.id AS source_id, si.id AS source_item_id, sir.id AS source_item_revision_id,
                           si.kind, si.author_name,
                           si.metadata->>'temporal_fidelity' AS temporal_fidelity,
                           si.metadata->>'raw_timestamp' AS raw_timestamp
                    FROM claims c
                    JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
                    JOIN source_items si ON si.id = sir.source_item_id
                    JOIN sources s ON s.id = si.source_id
                    LEFT JOIN publication_input_claims pic ON pic.publication_input_id = %s AND pic.claim_id = c.id
                    WHERE c.id = ANY(%s)
                    ORDER BY c.id ASC
                    """,
                    (inp.id, inp.claim_ids or [0]),
                )
                claim_rows = await c_cur.fetchall()

                card_source_refs: list[str] = []
                card_hard_facts: list[StoryElement] = []
                card_community_obs: list[StoryElement] = []

                from src.editorial_models import StoryElement

                for crow in claim_rows:
                    (
                        cid,
                        assertion,
                        norm_assertion,
                        platform,
                        src_name,
                        ext_id,
                        pub_at,
                        text_content,
                        canonical_url,
                        s_url,
                        s_ext_id,
                        s_role,
                        source_id,
                        source_item_id,
                        source_item_rev_id,
                        item_kind,
                        item_author_name,
                        temp_fidelity,
                        raw_ts,
                    ) = crow
                    ref_key = f"{platform}:source:{source_id}:item:{source_item_id}:rev:{source_item_rev_id}"
                    card_source_refs.append(ref_key)

                    is_comment = (item_kind in ("facebook_comment", "comment")) or str(
                        ext_id
                    ).startswith("comment:")
                    is_official = (s_role == "official") and not is_comment
                    container_name = src_name or platform
                    if is_comment:
                        author_label = item_author_name or "участник сообщества"
                        attribution_label = f"{author_label} ({container_name})"
                    else:
                        attribution_label = src_name or (
                            "официальные источники" if is_official else "сообщения жителей"
                        )

                    if ref_key not in records:
                        from src.collector import Message

                        if canonical_url:
                            link = canonical_url
                        elif platform == "telegram" and s_ext_id:
                            clean_chan = s_ext_id.lstrip("@")
                            clean_msg = str(ext_id).replace("msg:", "")
                            link = f"https://t.me/{clean_chan}/{clean_msg}"
                        elif s_url:
                            link = s_url
                        elif platform == "facebook":
                            clean_ext = str(ext_id).replace("post:", "").replace("comment:", "")
                            link = f"https://www.facebook.com/{clean_ext}"
                        else:
                            clean_ext = str(ext_id).replace("msg:", "")
                            link = (
                                f"https://t.me/{clean_ext}"
                                if clean_ext.isdigit()
                                else f"https://t.me/{clean_ext.lstrip('@')}"
                            )

                        msg_num = 0
                        raw_ext = str(ext_id).replace("msg:", "")
                        if raw_ext.isdigit():
                            msg_num = int(raw_ext)

                        msg_sender = (
                            attribution_label
                            if is_comment
                            else (item_author_name or src_name or platform)
                        )
                        msg = Message(
                            text=text_content or assertion,
                            sender=msg_sender,
                            timestamp=pub_at,
                            link=link,
                            channel_name=container_name,
                            has_media=False,
                            media_type="",
                            message_id=msg_num,
                            temporal_fidelity=temp_fidelity,
                            raw_timestamp=raw_ts,
                        )
                        records[ref_key] = SourceRecord(
                            ref=ref_key,
                            message=msg,
                            source_type=(
                                "comment"
                                if is_comment
                                else ("official" if is_official else "channel")
                            ),
                            context_text=text_content or assertion,
                        )

                    # Build StoryElement directly referencing this Claim's exact ref
                    if is_official:
                        card_hard_facts.append(
                            StoryElement(
                                text=assertion,
                                source_refs=[ref_key],
                                status="established",
                                attribution=attribution_label,
                            )
                        )
                    else:
                        card_community_obs.append(
                            StoryElement(
                                text=assertion,
                                source_refs=[ref_key],
                                status="attributed",
                                attribution=attribution_label,
                            )
                        )

                representative_refs = list(dict.fromkeys(card_source_refs))
                if projected_text:
                    card_summary = projected_text
                elif claim_rows:
                    claim_assertions = [
                        crow[2] or crow[1] for crow in claim_rows if (crow[2] or crow[1])
                    ]
                    card_summary = (
                        " ".join(dict.fromkeys(claim_assertions))
                        if claim_assertions
                        else semantic_text
                    )
                else:
                    card_summary = semantic_text

                if not card_hard_facts and not card_community_obs:
                    card_hard_facts = [
                        StoryElement(text=card_summary, source_refs=representative_refs)
                    ]

                card = StoryCard(
                    id=f"story-{inp.story_id}",
                    topic="Городские события",
                    importance="high" if rank == 1 else "medium",
                    summary=card_summary,
                    representative_source_refs=representative_refs,
                    hard_facts=card_hard_facts,
                    community_observations=card_community_obs,
                    useful_details=[],
                    uncertainties=[],
                )
                cards.append(card)

            def _format_record_header(r: SourceRecord) -> str:
                chan = getattr(r.message, "channel_name", None) or r.source_type
                ts = getattr(r.message, "timestamp", None)
                fidelity = getattr(r.message, "temporal_fidelity", None)
                raw_ts = getattr(r.message, "raw_timestamp", None)
                if ts:
                    if fidelity == "relative" and raw_ts:
                        ts_str = f"~{raw_ts} (approx)"
                    elif fidelity == "relative":
                        ts_str = (
                            f"~{ts.strftime('%Y-%m-%d %H:%M UTC')} (approx)"
                            if isinstance(ts, dt.datetime)
                            else f"~{ts} (approx)"
                        )
                    elif fidelity == "unknown" and raw_ts:
                        ts_str = f"{raw_ts} (approx)"
                    else:
                        ts_str = (
                            ts.strftime("%Y-%m-%d %H:%M UTC")
                            if isinstance(ts, dt.datetime)
                            else str(ts)
                        )
                    return f"[{r.ref} ({chan}, {ts_str})]"
                elif raw_ts:
                    return f"[{r.ref} ({chan}, {raw_ts})]"
                return f"[{r.ref} ({chan})]"

            bundle = PreparedBundle(
                records=records,
                prompt_text="\n\n".join(
                    f"{_format_record_header(r)} {r.context_text}" for r in records.values()
                ),
                total_messages=len(records),
                candidate_count=len(records),
            )
            analysis = EditorialAnalysis(cards=cards)
            return FrozenEditorialInput(analysis=analysis, writer_bundle=bundle, run_id=run_id)
