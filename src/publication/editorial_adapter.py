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

            ep_cur = await conn.execute(
                "SELECT config->'excluded_platforms' FROM eligibility_policy_versions WHERE id = %s",
                (run.eligibility_policy_id,),
            )
            ep_row = await ep_cur.fetchone()
            excluded_platforms = (
                ep_row[0] if (ep_row and isinstance(ep_row[0], list)) else []
            ) or []

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

                # Fetch claims and their sources
                c_cur = await conn.execute(
                    """
                    SELECT c.id, c.assertion_text, c.normalized_assertion,
                           s.platform, s.name, si.external_id, si.published_at,
                           sir.text_content, si.canonical_url, s.url, s.external_id, s.role,
                           s.id AS source_id, si.id AS source_item_id, sir.id AS source_item_revision_id,
                           si.kind, si.author_name
                    FROM claims c
                    JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
                    JOIN source_items si ON si.id = sir.source_item_id
                    JOIN sources s ON s.id = si.source_id
                    WHERE c.id = ANY(%s)
                    ORDER BY c.id ASC
                    """,
                    (inp.claim_ids or [0],),
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
                    ) = crow
                    ref_key = f"{platform}:source:{source_id}:item:{source_item_id}:rev:{source_item_rev_id}"
                    card_source_refs.append(ref_key)

                    is_official = s_role == "official"
                    is_comment = (item_kind in ("facebook_comment", "comment")) or str(
                        ext_id
                    ).startswith("comment:")
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
                            timestamp=pub_at or dt.datetime.now(dt.timezone.utc),
                            link=link,
                            channel_name=container_name,
                            has_media=False,
                            media_type="",
                            message_id=msg_num,
                        )
                        records[ref_key] = SourceRecord(
                            ref=ref_key,
                            message=msg,
                            source_type="official"
                            if is_official
                            else ("comment" if is_comment else "channel"),
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
                card_summary = semantic_text
                if excluded_platforms:
                    import re

                    for plat in excluded_platforms:
                        if plat == "facebook":
                            card_summary = re.sub(
                                r"https?://(?:www\.)?facebook\.com\S*",
                                "",
                                card_summary,
                                flags=re.IGNORECASE,
                            ).strip()

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

            bundle = PreparedBundle(
                records=records,
                prompt_text="\n\n".join(
                    f"[{r.ref} ({getattr(r.message, 'channel_name', r.source_type)})] {r.context_text}"
                    for r in records.values()
                ),
                total_messages=len(records),
                candidate_count=len(records),
            )
            analysis = EditorialAnalysis(cards=cards)
            return FrozenEditorialInput(analysis=analysis, writer_bundle=bundle, run_id=run_id)
