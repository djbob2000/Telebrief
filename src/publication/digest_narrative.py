"""Deterministic block planning, models, validation, and single-call writer for Event-First narrative digests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.editorial_models import StoryCard
from src.publication.article_claims import ConcreteClaim, find_unsupported_claims
from src.publication.evidence import PublicationEvidence

_INTERNAL_LEAKAGE_RE = re.compile(r"\[(?:story:\d+|SUPPORT\s+\d+|ref-\d+|tg:\S+)\]", re.IGNORECASE)


@dataclass(frozen=True)
class DigestNarrativeValidationResult:
    """Outcome of validating a narrative digest draft against a deterministic plan."""

    is_valid: bool
    violations: tuple[str, ...]
    unsupported_claims: tuple[ConcreteClaim, ...]


@dataclass(frozen=True)
class DigestNarrativeBlock:
    """Immutable presentation block grouping a fixed subset of rubric story cards."""

    block_id: str
    rubric_id: str
    rubric_title: str
    story_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    canonical_notes: tuple[str, ...]


@dataclass(frozen=True)
class DigestNarrativePlan:
    """Deterministic plan of immutable narrative digest blocks."""

    blocks: tuple[DigestNarrativeBlock, ...]


@dataclass(frozen=True)
class DigestNarrativeParagraph:
    """A single editorial paragraph within a narrative digest block."""

    text: str
    cited_support_ids: tuple[str, ...] = ()
    covered_story_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DigestNarrativeBlockDraft:
    """A single rendered block in a narrative digest draft."""

    block_id: str
    heading: str
    paragraphs: tuple[DigestNarrativeParagraph, ...]


@dataclass(frozen=True)
class DigestNarrativeDraft:
    """Complete output draft from the single-call narrative digest writer."""

    blocks: tuple[DigestNarrativeBlockDraft, ...]

    @classmethod
    def from_dict(cls, data: Any) -> DigestNarrativeDraft:
        """Parse structured narrative digest draft with strict structural validation."""
        if not isinstance(data, Mapping):
            raise ValueError("root must be a mapping")

        raw_blocks = data.get("blocks")
        if raw_blocks is None:
            raise ValueError("missing 'blocks' list")
        if not isinstance(raw_blocks, list):
            raise ValueError("'blocks' must be a list")

        seen_block_ids: set[str] = set()
        block_drafts: list[DigestNarrativeBlockDraft] = []

        for b in raw_blocks:
            if not isinstance(b, Mapping):
                raise ValueError("block item must be a mapping")

            block_id = str(b.get("block_id") or "").strip()
            if not block_id:
                raise ValueError("missing or empty 'block_id'")
            if block_id in seen_block_ids:
                raise ValueError(f"duplicate block_id: {block_id}")
            seen_block_ids.add(block_id)

            heading = str(b.get("heading") or "").strip()
            raw_paras = b.get("paragraphs")
            if raw_paras is None or not isinstance(raw_paras, list) or len(raw_paras) == 0:
                raise ValueError(f"block {block_id} must contain at least one paragraph")

            para_drafts: list[DigestNarrativeParagraph] = []
            for p in raw_paras:
                if not isinstance(p, Mapping):
                    raise ValueError("paragraph must be a mapping")

                text = str(p.get("text") or "").strip()
                if not text:
                    raise ValueError(f"paragraph text cannot be empty in block {block_id}")

                raw_supports = p.get("cited_support_ids", [])
                if isinstance(raw_supports, str):
                    raw_supports = [raw_supports]
                if not isinstance(raw_supports, list):
                    raise ValueError(f"cited_support_ids must be a list in block {block_id}")
                cited_supports = tuple(str(s).strip() for s in raw_supports if str(s).strip())

                raw_stories = p.get("covered_story_ids", [])
                if isinstance(raw_stories, str):
                    raw_stories = [raw_stories]
                if not isinstance(raw_stories, list):
                    raise ValueError(f"covered_story_ids must be a list in block {block_id}")
                covered_stories = tuple(str(s).strip() for s in raw_stories if str(s).strip())

                para_drafts.append(
                    DigestNarrativeParagraph(
                        text=text,
                        cited_support_ids=cited_supports,
                        covered_story_ids=covered_stories,
                    )
                )

            block_drafts.append(
                DigestNarrativeBlockDraft(
                    block_id=block_id,
                    heading=heading,
                    paragraphs=tuple(para_drafts),
                )
            )

        return cls(blocks=tuple(block_drafts))


def plan_digest_narrative_blocks(
    *,
    cards: Sequence[StoryCard],
    evidence: Mapping[str, PublicationEvidence],
    rubrics: Sequence[Any],
    max_cards_per_block: int = 6,
) -> DigestNarrativePlan:
    """Build immutable narrative blocks from classified story cards strictly preserving order."""
    if not cards:
        return DigestNarrativePlan(blocks=())

    def _get_r_info(r: Any) -> tuple[str, str, bool]:
        if isinstance(r, Mapping):
            return (
                str(r.get("id", "")),
                str(r.get("title") or r.get("name") or ""),
                bool(r.get("fallback", False)),
            )
        return (
            str(getattr(r, "id", "")),
            str(getattr(r, "name", "")),
            bool(getattr(r, "fallback", False)),
        )

    rubric_infos = [_get_r_info(r) for r in rubrics]
    rubric_ids = [info[0] for info in rubric_infos if info[0]]
    fallback_info = next(
        (info for info in rubric_infos if info[2]),
        rubric_infos[0] if rubric_infos else ("other", "Другое", True),
    )
    fallback_id = fallback_info[0] if fallback_info[0] else "other"

    # Group cards by rubric, preserving rubric sequence
    cards_by_rubric: dict[str, list[StoryCard]] = {rid: [] for rid in rubric_ids}
    for card in cards:
        rid = card.rubric_id if card.rubric_id in cards_by_rubric else fallback_id
        if rid not in cards_by_rubric:
            cards_by_rubric[rid] = []
        cards_by_rubric[rid].append(card)

    blocks: list[DigestNarrativeBlock] = []

    for rid, rname, _ in rubric_infos:
        if not rid:
            continue
        rubric_cards = cards_by_rubric.get(rid, [])
        if not rubric_cards:
            continue

        bound = max(1, max_cards_per_block)
        for chunk_idx in range(0, len(rubric_cards), bound):
            chunk = rubric_cards[chunk_idx : chunk_idx + bound]
            block_id = f"block:{rid}:{chunk_idx // bound}"
            story_ids = tuple(c.id for c in chunk)

            # Collect canonical notes from cards
            notes: list[str] = []
            block_support_ids: list[str] = []
            for c in chunk:
                if c.summary:
                    notes.append(f"{c.topic}: {c.summary}")
                elif c.topic:
                    notes.append(c.topic)
                for hf in c.hard_facts:
                    if hf.text and hf.text not in notes:
                        notes.append(hf.text)
                for co in c.community_observations:
                    if co.text and co.text not in notes:
                        notes.append(co.text)

                # Extract numeric story ID if story:123
                if c.id.startswith("story:"):
                    raw_sid = c.id.split(":", 1)[1]
                    if raw_sid.isdigit():
                        num_sid = int(raw_sid)
                        for eid, evi in evidence.items():
                            if (
                                getattr(evi, "story_id", None) == num_sid
                                and eid not in block_support_ids
                            ):
                                block_support_ids.append(eid)

            blocks.append(
                DigestNarrativeBlock(
                    block_id=block_id,
                    rubric_id=rid,
                    rubric_title=rname,
                    story_ids=story_ids,
                    support_ids=tuple(block_support_ids),
                    canonical_notes=tuple(notes),
                )
            )

    return DigestNarrativePlan(blocks=tuple(blocks))


def validate_digest_narrative(
    draft: DigestNarrativeDraft,
    plan: DigestNarrativePlan,
    *,
    support_text_by_id: Mapping[str, str],
) -> DigestNarrativeValidationResult:
    """Validate structured narrative digest draft strictly against deterministic plan and evidence."""
    violations: list[str] = []
    unsupported_claims: list[ConcreteClaim] = []

    plan_blocks_by_id = {b.block_id: b for b in plan.blocks}
    draft_block_ids = [b.block_id for b in draft.blocks]
    plan_block_ids = [b.block_id for b in plan.blocks]

    if len(draft.blocks) != len(plan.blocks):
        violations.append(
            f"BLOCK_COUNT_MISMATCH: expected {len(plan.blocks)} blocks, got {len(draft.blocks)}"
        )

    if draft_block_ids != plan_block_ids:
        violations.append(
            f"BLOCK_ID_SEQUENCE_MISMATCH: expected {plan_block_ids}, got {draft_block_ids}"
        )

    seen_story_ids: set[str] = set()

    for block_draft in draft.blocks:
        plan_block = plan_blocks_by_id.get(block_draft.block_id)
        if plan_block is None:
            violations.append(f"UNKNOWN_BLOCK_ID: {block_draft.block_id}")
            continue

        allowed_supports = set(plan_block.support_ids)
        allowed_stories = set(plan_block.story_ids)
        covered_stories_in_block: set[str] = set()

        for para in block_draft.paragraphs:
            if _INTERNAL_LEAKAGE_RE.search(para.text):
                violations.append(
                    f"INTERNAL_LEAKAGE: found internal identifier in block {block_draft.block_id}"
                )

            if not para.cited_support_ids:
                violations.append(
                    f"MISSING_SUPPORT_CITATION: paragraph in block {block_draft.block_id} cites no supports"
                )

            for sup_id in para.cited_support_ids:
                if sup_id not in allowed_supports and allowed_supports:
                    violations.append(
                        f"DISALLOWED_SUPPORT_ID: {sup_id} not allowed in block {block_draft.block_id}"
                    )
                if sup_id not in support_text_by_id:
                    violations.append(
                        f"UNKNOWN_SUPPORT_ID: {sup_id} not found in support text index"
                    )

            for sid in para.covered_story_ids:
                if sid not in allowed_stories:
                    violations.append(
                        f"DISALLOWED_STORY_ID: {sid} not allowed in block {block_draft.block_id}"
                    )
                covered_stories_in_block.add(sid)
                seen_story_ids.add(sid)

            # Validate concrete claims against cited support texts
            c_supports = [
                support_text_by_id[s] for s in para.cited_support_ids if s in support_text_by_id
            ]
            para_unsupported = find_unsupported_claims(para.text, c_supports)
            for unc in para_unsupported:
                unsupported_claims.append(unc)
                violations.append(
                    f"UNSUPPORTED_CONCRETE_CLAIM: [{unc.kind}] '{unc.raw}' in block {block_draft.block_id}"
                )

        uncovered_stories = allowed_stories - covered_stories_in_block
        for unc_sid in uncovered_stories:
            violations.append(
                f"UNCOVERED_STORY: {unc_sid} was not covered in block {block_draft.block_id}"
            )

    is_valid = len(violations) == 0 and len(unsupported_claims) == 0
    return DigestNarrativeValidationResult(
        is_valid=is_valid,
        violations=tuple(violations),
        unsupported_claims=tuple(unsupported_claims),
    )


class DigestNarrativeWriter:
    """Single-call narrative digest writer synthesizing flowing prose across rubric blocks."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def generate_narrative_draft(
        self,
        *,
        plan: DigestNarrativePlan,
        cards: Sequence[StoryCard],
        evidence: Mapping[str, PublicationEvidence],
        situation_rollup: Any | None = None,
        language: str = "Russian",
        max_output_tokens: int = 4096,
        model: str | None = None,
    ) -> DigestNarrativeDraft:
        """Synthesize structured narrative draft in exactly one LLM call."""
        import json

        from src.publication.narrative_contract import build_digest_narrative_contract

        blocks_payload = []
        for b in plan.blocks:
            supports_payload = []
            for sid in b.support_ids:
                if sid in evidence:
                    evi = evidence[sid]
                    supports_payload.append(
                        {
                            "id": sid,
                            "text": evi.text,
                            "role": evi.source_role,
                        }
                    )
            blocks_payload.append(
                {
                    "block_id": b.block_id,
                    "rubric_id": b.rubric_id,
                    "rubric_title": b.rubric_title,
                    "story_ids": list(b.story_ids),
                    "canonical_notes": list(b.canonical_notes),
                    "supports": supports_payload,
                }
            )

        narrative_contract = build_digest_narrative_contract(output_language=language)
        system_prompt = (
            "You are a professional regional newsroom editor and journalist.\n"
            "Your task is to write a cohesive, engaging, and strictly factual daily news digest.\n\n"
            f"{narrative_contract}\n\n"
            "OUTPUT FORMAT REQUIREMENTS:\n"
            "Return ONLY valid JSON strictly matching this schema:\n"
            "{\n"
            '  "blocks": [\n'
            "    {\n"
            '      "block_id": "string (must match input block_id exactly)",\n'
            '      "heading": "string (clear section heading)",\n'
            '      "paragraphs": [\n'
            "        {\n"
            '          "text": "string (flowing editorial prose synthesizing events)",\n'
            '          "cited_support_ids": ["string (support IDs)"],\n'
            '          "covered_story_ids": ["string (story IDs covered)"]\n'
            "        }\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
        )
        user_prompt = json.dumps({"blocks": blocks_payload}, ensure_ascii=False, indent=2)

        chat_kwargs: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        if model:
            chat_kwargs["model"] = model

        raw_response = await self._provider.chat_completion(**chat_kwargs)

        cleaned = (raw_response or "").strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        if "{" in cleaned and "}" in cleaned:
            first_brace = cleaned.find("{")
            last_brace = cleaned.rfind("}")
            if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                cleaned = cleaned[first_brace : last_brace + 1]

        try:
            parsed = json.loads(cleaned)
        except Exception as err:
            raise ValueError(
                f"Failed to decode LLM response as JSON: {err}. Raw was: {raw_response[:200]!r}"
            ) from err

        return DigestNarrativeDraft.from_dict(parsed)


def build_digest_support_text_index(
    *,
    evidence: Mapping[str, PublicationEvidence],
    cards: Sequence[StoryCard],
    frozen_input: Any | None = None,
) -> dict[str, str]:
    """Build unified mapping from support IDs and synthesized card IDs to exact support texts."""
    index: dict[str, str] = {}

    # 1. Primary publication evidence items
    for eid, evi in evidence.items():
        if getattr(evi, "text", None):
            index[eid] = evi.text
        elif getattr(evi, "source_text", None):
            index[eid] = evi.source_text

    # 2. Frozen input writer records if present
    if frozen_input is not None and getattr(frozen_input, "writer_bundle", None):
        records = getattr(frozen_input.writer_bundle, "records", {})
        if isinstance(records, dict):
            for ref, rec in records.items():
                msg = getattr(rec, "message", None)
                msg_text = getattr(msg, "text", "") if msg else ""
                if msg_text and ref not in index:
                    index[ref] = msg_text

    # 3. Card-level canonical notes and elements
    for c in cards:
        card_texts: list[str] = []
        if c.topic:
            card_texts.append(c.topic)
        if c.summary:
            card_texts.append(c.summary)
        for hf in c.hard_facts:
            if hf.text:
                card_texts.append(hf.text)
                for r in hf.source_refs:
                    if r not in index and hf.text:
                        index[r] = hf.text
        for ud in c.useful_details:
            if ud.text:
                card_texts.append(ud.text)
                for r in ud.source_refs:
                    if r not in index and ud.text:
                        index[r] = ud.text
        for co in c.community_observations:
            if co.text:
                card_texts.append(co.text)
                for r in co.source_refs:
                    if r not in index and co.text:
                        index[r] = co.text

        if card_texts and c.id not in index:
            index[c.id] = " ".join(card_texts)

    return index
