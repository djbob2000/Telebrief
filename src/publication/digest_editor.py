"""Editorial copy-editor, literary polisher, and compression engine for narrative digests."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Sequence

from src.ai_providers import AIProvider
from src.publication.digest_narrative import (
    DigestClaimAtom,
    DigestEditorialItemDraft,
    DigestNarrativeBlockDraft,
    DigestNarrativeDraft,
    sanitize_digest_narrative_draft,
)
from src.publication.evidence import PublicationEvidence

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class DigestEditor:
    """Refines, polishes, and compresses narrative digest drafts for Telegram single-post publication."""

    def __init__(self, provider: AIProvider | None = None) -> None:
        self._provider = provider

    async def polish_and_compress(
        self,
        draft: DigestNarrativeDraft,
        *,
        plan: Any = None,
        evidence: Mapping[str, PublicationEvidence] | None = None,
        max_chars: int = 3600,
        model: str | None = None,
        violations: Sequence[str] | None = None,
    ) -> DigestNarrativeDraft:
        """Apply targeted journalistic polish, contrast synthesis, and length compression."""
        if self._provider is None:
            logger.warning("No AI provider available for DigestEditor; returning original draft")
            return draft

        # Build structured items payload for the editor model
        blocks_payload: list[dict[str, Any]] = []
        for block in draft.blocks:
            items_payload: list[dict[str, Any]] = []
            for it_idx, item in enumerate(block.items):
                items_payload.append(
                    {
                        "item_index": it_idx,
                        "emoji": item.emoji,
                        "headline": item.headline,
                        "body": item.body,
                    }
                )
            blocks_payload.append(
                {
                    "block_id": block.block_id,
                    "items": items_payload,
                }
            )

        repair_section = ""
        if violations:
            v_list = "\n".join(f"- {v}" for v in violations[:10])
            repair_section = (
                "\nCRITICAL VALIDATION REPAIRS REQUIRED:\n"
                "The draft failed automated editorial validation with the following violations:\n"
                f"{v_list}\n"
                "- If a fact, story claim, or required mention is missing, smoothly integrate the missing information into the relevant block's item body.\n"
                "- If a statement was unverified, over-specified, or unsupported, tone it down or state it faithfully.\n"
                "- Do NOT drop facts or invent unsupported new details.\n\n"
            )

        system_prompt = (
            "You are a chief copy-editor of a respected regional Telegram news channel.\n"
            "Your task is to refine, polish, and tighten a daily city news digest in Russian.\n\n"
            f"{repair_section}"
            "EDITORIAL PRINCIPLES:\n"
            "1. REFINED JOURNALISTIC STYLE:\n"
            "   - Transform choppy, fragmented, or dry bureaucratic phrases into smooth, engaging, and professional Russian prose.\n"
            "   - Remove repetitive attributions ('По сообщениям жителей', 'жители сообщают') - at most ONE natural attribution per item, or state facts directly.\n"
            "   - Smoothly synthesize localized contrasts across streets (e.g. 'в нагорной части ситуация неоднородная: на одних улицах..., тогда как на других...').\n"
            "   - Eliminate all chat debris, Telegram mechanics, or forum meta-language ('в чатах', 'участники переклички', 'паблики', emoji spam).\n\n"
            "2. SCAN-FIRST INFORMATIVE HEADLINES:\n"
            "   - Every item MUST have a specific, informative, scan-friendly headline with an emoji.\n"
            "   - STRICTLY FORBIDDEN: generic placeholder headlines like 'Городские события', 'Новости города', 'Информация', 'События дня'.\n"
            "   - State the specific subject or neighborhood (e.g. '⚡️ Отключения на ул. Пионерской и бульваре Гайдара', '💧 Водовод в районе АКЗ').\n\n"
            "3. SINGLE-POST TELEGRAM BUDGET (COMPRESSION):\n"
            f"   - The entire combined text across all items MUST fit comfortably within {max_chars} characters.\n"
            "   - Remove wordiness, redundant descriptions, and duplicate mentions across items.\n"
            "   - NEVER drop concrete facts, micro-locations (streets, buildings), numbers, hours, or names of public services/contractors.\n"
            "   - NEVER invent unverified facts, causes, or advice.\n\n"
            "OUTPUT FORMAT:\n"
            "Return valid JSON strictly matching this structure:\n"
            "{\n"
            '  "blocks": [\n'
            "    {\n"
            '      "block_id": "string",\n'
            '      "items": [\n'
            "        {\n"
            '          "item_index": 0,\n'
            '          "emoji": "⚡️",\n'
            '          "headline": "...",\n'
            '          "body": "..."\n'
            "        }\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
        )

        user_content: dict[str, Any] = {"blocks": blocks_payload}
        if violations:
            user_content["validation_violations_to_fix"] = list(violations[:10])
        user_prompt = json.dumps(user_content, ensure_ascii=False, indent=2)

        chat_kwargs: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "reasoning_effort": "none",
            "thinking": False,
        }
        if model:
            chat_kwargs["model"] = model
        chat_kwargs["max_tokens"] = 4096

        try:
            raw_response = await self._provider.chat_completion(**chat_kwargs)
            text_resp = (raw_response or "").strip()
            json_match = _JSON_BLOCK_RE.search(text_resp)
            if json_match:
                parsed = json.loads(json_match.group(1))
            else:
                first_brace = text_resp.find("{")
                last_brace = text_resp.rfind("}")
                if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                    parsed = json.loads(text_resp[first_brace : last_brace + 1])
                else:
                    parsed = json.loads(text_resp)

            if not isinstance(parsed, dict) or "blocks" not in parsed:
                logger.warning("DigestEditor output missing 'blocks'; returning original draft")
                return draft

            # Merge polished text back into draft items
            revised_blocks: list[DigestNarrativeBlockDraft] = []
            for orig_block in draft.blocks:
                matching_p_block = next(
                    (
                        pb
                        for pb in parsed["blocks"]
                        if isinstance(pb, dict) and pb.get("block_id") == orig_block.block_id
                    ),
                    None,
                )
                if not matching_p_block or not isinstance(matching_p_block.get("items"), list):
                    revised_blocks.append(orig_block)
                    continue

                p_items_by_idx = {
                    int(it["item_index"]): it
                    for it in matching_p_block["items"]
                    if isinstance(it, dict) and "item_index" in it
                }

                plan_block = None
                if plan is not None and getattr(plan, "blocks", None):
                    plan_block = next(
                        (b for b in plan.blocks if b.block_id == orig_block.block_id),
                        None,
                    )

                new_items: list[DigestEditorialItemDraft] = []
                for it_idx, orig_item in enumerate(orig_block.items):
                    p_it = p_items_by_idx.get(it_idx)
                    if p_it and p_it.get("body") and p_it.get("headline"):
                        new_head = str(p_it["headline"]).strip()
                        new_body = str(p_it["body"]).strip()
                        new_emoji = str(p_it.get("emoji") or orig_item.emoji or "").strip()

                        item_claims = list(orig_item.claims)
                        item_sups = list(orig_item.cited_support_ids)
                        covered_fids_in_item = {
                            fid for c in item_claims for fid in getattr(c, "covered_fact_ids", ())
                        }

                        if plan_block and getattr(plan_block, "required_facts", None):
                            allowed_block_supports = set(getattr(plan_block, "support_ids", ()))
                            norm_body = new_body.replace("ё", "е").lower()
                            for rf in plan_block.required_facts:
                                if rf.fact_id in covered_fids_in_item:
                                    continue
                                rf_tokens = set(rf.fact_id.replace("ё", "е").lower().split("_")) - {
                                    "бердянск",
                                    "ул",
                                    "улица",
                                    "район",
                                    "часть",
                                    "город",
                                    "г",
                                }
                                claim_story_ids = tuple(
                                    s for s in rf.story_ids if s in orig_item.covered_story_ids
                                )
                                if not claim_story_ids:
                                    continue

                                is_match = (
                                    len(orig_block.items) == 1
                                    or bool(set(rf.story_ids) & set(orig_item.covered_story_ids))
                                    or (
                                        bool(rf_tokens)
                                        and any(
                                            tok in norm_body for tok in rf_tokens if len(tok) >= 3
                                        )
                                    )
                                )
                                if is_match:
                                    rf_sups: list[str] = []
                                    for sid in claim_story_ids:
                                        story_allowed = set(
                                            dict(plan_block.support_ids_by_story).get(sid, ())
                                        )
                                        matching = [s for s in rf.support_ids if s in story_allowed]
                                        if matching:
                                            rf_sups.extend(matching)
                                        elif story_allowed:
                                            rf_sups.extend(sorted(story_allowed)[:1])
                                    rf_sups = list(dict.fromkeys(rf_sups))
                                    if not rf_sups:
                                        rf_sups = [
                                            s for s in rf.support_ids if s in allowed_block_supports
                                        ] or list(rf.support_ids)

                                    item_claims.append(
                                        DigestClaimAtom(
                                            text=rf.text or new_head,
                                            covered_story_ids=claim_story_ids,
                                            cited_support_ids=tuple(rf_sups),
                                            covered_fact_ids=(rf.fact_id,),
                                        )
                                    )
                                    item_sups.extend(rf_sups)
                                    covered_fids_in_item.add(rf.fact_id)

                        new_items.append(
                            DigestEditorialItemDraft(
                                headline=new_head,
                                body=new_body,
                                covered_story_ids=orig_item.covered_story_ids,
                                cited_support_ids=tuple(dict.fromkeys(item_sups)),
                                claims=tuple(item_claims),
                                emoji=new_emoji,
                            )
                        )
                    else:
                        new_items.append(orig_item)

                revised_blocks.append(
                    DigestNarrativeBlockDraft(
                        block_id=orig_block.block_id,
                        items=tuple(new_items),
                    )
                )

            polished_draft = DigestNarrativeDraft(
                blocks=tuple(revised_blocks),
                situation_items=draft.situation_items,
            )
            return sanitize_digest_narrative_draft(polished_draft)

        except Exception as exc:
            logger.warning(
                "DigestEditor polish_and_compress failed (%s: %s); returning original draft",
                type(exc).__name__,
                exc,
            )
            return draft
