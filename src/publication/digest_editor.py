"""Editorial copy-editor, literary polisher, and compression engine for narrative digests."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from typing import Any, Mapping, Sequence

from src.ai_providers import AIProvider
from src.publication.digest_narrative import (
    DigestClaimAtom,
    DigestEditorialItemDraft,
    DigestNarrativeBlockDraft,
    DigestNarrativeDraft,
    _parse_composition_writer_output,
    _publish_support_texts,
    _same_fact_group_merge_id,
    _same_fact_merge_id,
    sanitize_digest_narrative_draft,
)
from src.publication.digest_quality_diagnostics import (
    MAX_POWER_REPORT_ITEMS_PER_BLOCK,
)
from src.publication.evidence import PublicationEvidence

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class DigestRecompositionError(ValueError):
    """Safe-to-retry structural rejection with no source prose attached."""


def _resolve_approved_merges(
    *,
    draft: DigestNarrativeDraft,
    plan: Any,
    item_locations: Mapping[str, tuple[DigestNarrativeBlockDraft, DigestEditorialItemDraft]],
    allowed_merges: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Resolve exact pair or connected-graph SAME_FACT authorizations."""
    plan_blocks = {block.block_id: block for block in plan.blocks}
    approved: dict[str, list[dict[str, Any]]] = {}
    reserved_items: set[str] = set()
    for merge_id, authorization in allowed_merges.items():
        if isinstance(authorization, Mapping):
            raw_item_ids = authorization.get(
                "source_item_ids", authorization.get("input_item_ids", ())
            )
            raw_relation_ids = authorization.get("relation_ids", ())
            if not raw_relation_ids and isinstance(authorization.get("relations"), list):
                raw_relation_ids = [
                    str(
                        edge.get("relation_id")
                        or _same_fact_merge_id(
                            str(edge.get("left_fact_id", "")),
                            str(edge.get("right_fact_id", "")),
                        )
                    )
                    for edge in authorization["relations"]
                    if isinstance(edge, Mapping)
                    and str(edge.get("kind", "SAME_FACT")).upper() == "SAME_FACT"
                ]
            source_unit_ids = tuple(
                str(value) for value in authorization.get("source_unit_ids", ())
            )
            declared_rubric = str(authorization.get("rubric_id", ""))
        else:
            # Compatibility form: merge ID names one SAME_FACT edge and value
            # gives its exact pair of source item IDs.
            raw_item_ids = authorization
            raw_relation_ids = (str(merge_id),)
            source_unit_ids = ()
            declared_rubric = ""
        if isinstance(raw_item_ids, (str, int)):
            raise ValueError(f"{merge_id}: source item IDs must be a sequence")
        item_ids = tuple(str(value) for value in raw_item_ids)
        relation_ids = tuple(str(value) for value in raw_relation_ids)
        if len(item_ids) < 2 or len(item_ids) != len(set(item_ids)):
            raise ValueError(f"{merge_id}: merge must name at least two distinct source items")
        if any(item_id not in item_locations for item_id in item_ids):
            raise ValueError(f"{merge_id}: unknown source item ID")
        if any(item_id in reserved_items for item_id in item_ids):
            raise ValueError("a source item cannot participate in multiple approved merges")
        source_blocks = [item_locations[item_id][0] for item_id in item_ids]
        if len({block.block_id for block in source_blocks}) != 1:
            raise ValueError(f"{merge_id}: source items must be in the same block")
        block_id = source_blocks[0].block_id
        plan_block = plan_blocks.get(block_id)
        if plan_block is None or not getattr(plan_block, "composition_units", ()):
            raise ValueError(f"{merge_id}: missing frozen composition block")
        all_relations = list(getattr(plan_block, "composition_relations", ()) or ())
        for unit in plan_block.composition_units:
            all_relations.extend(getattr(unit, "allowed_relations", ()) or ())
        same_fact_by_id = {
            _same_fact_merge_id(relation.left_fact_id, relation.right_fact_id): relation
            for relation in all_relations
            if str(getattr(relation.kind, "value", relation.kind)) == "SAME_FACT"
        }
        if not relation_ids or len(relation_ids) != len(set(relation_ids)):
            raise ValueError(f"{merge_id}: relation IDs must be nonempty and unique")
        edge_list = [same_fact_by_id.get(relation_id) for relation_id in relation_ids]
        if any(edge is None for edge in edge_list):
            raise ValueError(f"{merge_id}: every edge must resolve to a SAME_FACT relation")
        expected_merge_id = (
            relation_ids[0]
            if len(item_ids) == 2 and len(relation_ids) == 1
            else _same_fact_group_merge_id(item_ids, relation_ids)
        )
        if str(merge_id) != expected_merge_id:
            raise ValueError(f"{merge_id}: ID does not match its exact item and relation sets")

        source_items = [item_locations[item_id][1] for item_id in item_ids]
        actual_unit_ids = tuple(
            dict.fromkeys(
                unit_id
                for item in source_items
                for unit_id in (
                    item.composition_unit_ids
                    or ((item.composition_unit_id,) if item.composition_unit_id else ())
                )
            )
        )
        unit_map = {str(unit.unit_id): unit for unit in plan_block.composition_units}
        if source_unit_ids and set(source_unit_ids) != set(actual_unit_ids):
            raise ValueError(f"{merge_id}: source unit IDs do not match the exact inputs")
        if len(item_ids) > 2 and (not source_unit_ids or not declared_rubric):
            raise ValueError(f"{merge_id}: graph merge must declare source units and common rubric")
        if declared_rubric and declared_rubric != plan_block.rubric_id:
            raise ValueError(f"{merge_id}: declared common rubric does not match its block")
        fact_owner: dict[str, str] = {}
        for item_id, item in zip(item_ids, source_items, strict=True):
            if not item.covered_fact_ids or any(
                unit_id not in unit_map or not unit_map[unit_id].fact_ids
                for unit_id in actual_unit_ids
            ):
                raise ValueError(f"{merge_id}: SAME_FACT merges require fact-bearing inputs")
            for fact_id in item.covered_fact_ids:
                if fact_id in fact_owner:
                    raise ValueError(f"{merge_id}: source fact sets overlap")
                fact_owner[fact_id] = item_id
        graph: dict[str, set[str]] = {item_id: set() for item_id in item_ids}
        endpoint_facts: list[str] = []
        for edge in edge_list:
            if edge is None:
                raise ValueError(f"{merge_id}: every edge must resolve to a SAME_FACT relation")
            left_fact, right_fact = str(edge.left_fact_id), str(edge.right_fact_id)
            left_owner, right_owner = fact_owner.get(left_fact), fact_owner.get(right_fact)
            if not left_owner or not right_owner or left_owner == right_owner:
                raise ValueError(f"{merge_id}: each SAME_FACT edge must join distinct source items")
            graph[left_owner].add(right_owner)
            graph[right_owner].add(left_owner)
            endpoint_facts.extend((left_fact, right_fact))
        reached: set[str] = set()
        pending = [item_ids[0]]
        while pending:
            node = pending.pop()
            if node in reached:
                continue
            reached.add(node)
            pending.extend(graph[node])
        if reached != set(item_ids):
            raise ValueError(f"{merge_id}: SAME_FACT graph is not connected")
        reserved_items.update(item_ids)
        approved.setdefault(block_id, []).append(
            {
                "merge_id": str(merge_id),
                "source_item_ids": list(item_ids),
                "relation_ids": list(relation_ids),
                "source_unit_ids": list(actual_unit_ids),
                "rubric_id": plan_block.rubric_id,
                "endpoint_fact_ids": list(dict.fromkeys(endpoint_facts)),
            }
        )
    return approved


class DigestEditor:
    """Refines, polishes, and compresses narrative digest drafts for Telegram single-post publication."""

    def __init__(self, provider: AIProvider | None = None) -> None:
        self._provider = provider

    async def _polish_composition(
        self,
        draft: DigestNarrativeDraft,
        *,
        plan: Any,
        evidence: Mapping[str, PublicationEvidence],
        max_chars: int,
        model: str | None,
        violations: Sequence[str] | None,
        target_item_ids: Sequence[str] | None,
        allowed_merges: Mapping[str, Any],
        recompose_block_ids: Sequence[str] = (),
    ) -> DigestNarrativeDraft:
        """Target text-only repair while keeping frozen provenance immutable."""
        provider = self._provider
        if provider is None:
            logger.warning("No AI provider available for DigestEditor; returning original draft")
            return draft
        publish_texts = _publish_support_texts(evidence)
        plan_blocks = {block.block_id: block for block in plan.blocks}
        item_locations: dict[str, tuple[DigestNarrativeBlockDraft, DigestEditorialItemDraft]] = {}
        for block in draft.blocks:
            for item in block.items:
                if not item.item_id or item.item_id in item_locations:
                    logger.warning(
                        "Composition editor requires unique stable item IDs; returning original draft"
                    )
                    return draft
                item_locations[item.item_id] = (block, item)

        target_ids = set(item_locations if target_item_ids is None else target_item_ids)
        if not target_ids.issubset(item_locations):
            logger.warning(
                "Composition editor received unknown target item IDs; returning original draft"
            )
            return draft
        if not target_ids:
            return draft

        try:
            approved_by_block = _resolve_approved_merges(
                draft=draft,
                plan=plan,
                item_locations=item_locations,
                allowed_merges=allowed_merges,
            )
        except (AttributeError, TypeError, ValueError, StopIteration) as exc:
            logger.warning("Invalid approved digest merge map (%s); returning original draft", exc)
            return draft

        recompose_ids = set(recompose_block_ids)
        if not recompose_ids.issubset(plan_blocks):
            return draft
        recompose_target_ids: set[str] = set()
        for block in draft.blocks:
            if block.block_id not in recompose_ids:
                continue
            unit_by_id = {
                str(unit.unit_id): unit for unit in plan_blocks[block.block_id].composition_units
            }
            for item in block.items:
                if item.item_id in target_ids and item.covered_fact_ids:
                    recompose_target_ids.add(item.item_id)
        if recompose_ids and not recompose_target_ids:
            return draft

        editor_blocks: list[dict[str, Any]] = []
        for block in draft.blocks:
            plan_block = plan_blocks.get(block.block_id)
            if plan_block is None:
                return draft
            unit_by_id = {str(unit.unit_id): unit for unit in plan_block.composition_units}
            record_by_fact = {
                str(record.fact_id): record for record in plan_block.composition_fact_records
            }
            fact_by_id = {str(fact.fact_id): fact for fact in plan_block.required_facts}
            raw_items: list[dict[str, Any]] = []
            for item in block.items:
                source_rows = []
                for support_id in item.cited_support_ids:
                    texts = publish_texts.get(support_id, ())
                    if not texts:
                        logger.warning(
                            "Composition editor lacks PUBLISH evidence for %s; returning original draft",
                            support_id,
                        )
                        return draft
                    direct = evidence.get(support_id)
                    source_rows.append(
                        {
                            "support_id": support_id,
                            "texts": list(texts),
                            "evidence_kind": str(getattr(direct, "kind", "")),
                            "source_role": str(getattr(direct, "source_role", "")),
                            "publication_use": "PUBLISH",
                        }
                    )
                facts = []
                for fact_id in item.covered_fact_ids:
                    record = record_by_fact.get(fact_id)
                    fact = fact_by_id.get(fact_id)
                    if record is None or fact is None:
                        return draft
                    facts.append(
                        {
                            "fact_id": fact_id,
                            "text": fact.text,
                            "story_ids": list(record.story_ids),
                            "epistemic_kind": record.epistemic_kind,
                            "original_location": record.original_location,
                            "canonical_area": record.canonical_area,
                            "effective_time": record.effective_time.isoformat()
                            if record.effective_time
                            else None,
                            "observed_time": record.observed_time.isoformat()
                            if record.observed_time
                            else None,
                        }
                    )
                summary_units = [
                    {
                        "composition_unit_id": unit_id,
                        "summary_only_story_ids": list(unit_by_id[unit_id].story_ids),
                        "targeted_for_recomposition": item.item_id in recompose_target_ids,
                    }
                    for unit_id in (
                        item.composition_unit_ids
                        or ((item.composition_unit_id,) if item.composition_unit_id else ())
                    )
                    if unit_id in unit_by_id and not unit_by_id[unit_id].fact_ids
                ]
                raw_items.append(
                    {
                        "item_id": item.item_id,
                        "composition_unit_ids": list(
                            item.composition_unit_ids
                            or ((item.composition_unit_id,) if item.composition_unit_id else ())
                        ),
                        "covered_fact_ids": list(item.covered_fact_ids),
                        "covered_story_ids": list(item.covered_story_ids),
                        "cited_support_ids": list(item.cited_support_ids),
                        "claims": [claim.to_dict() for claim in item.claims],
                        "facts": facts,
                        "summary_units": summary_units,
                        "supports": source_rows,
                        "emoji": item.emoji,
                        "headline": item.headline,
                        "body": item.body,
                        "targeted_for_recomposition": item.item_id in recompose_target_ids,
                    }
                )
            editor_blocks.append(
                {
                    "block_id": block.block_id,
                    "rubric_id": plan_block.rubric_id,
                    "items": raw_items,
                    "allowed_merges": approved_by_block.get(block.block_id, []),
                    "allow_recomposition": block.block_id in recompose_ids,
                }
            )

        requested_findings = list(dict.fromkeys(violations or ()))
        # Retry feedback and evidence-preservation constraints are appended by
        # the caller. They must survive the limit on ordinary audit findings.
        prompt_findings = requested_findings[:10] + [
            finding
            for finding in requested_findings[10:]
            if finding.startswith("EDITORIAL_CONSTRAINT:")
        ]
        repair_lines = [f"- {finding}" for finding in prompt_findings]
        system_prompt = (
            "You are a careful local-news copy editor. Polish only the requested digest item text.\n"
            "Use the exact PUBLISH evidence and fact mapping supplied beside each item. One legitimate single-source community report may be included as a report; preserve natural attribution and uncertainty. Do not require a second source or official confirmation. Correct invented details, unsupported specifics, causal upgrades, and epistemic upgrades, but do not remove an eligible report merely because it is unconfirmed.\n"
            "In text-only patches, each item's unit/fact/story/support/claim mapping is immutable: change only headline/body/emoji for existing item IDs. Explicitly authorized recomposition below may regroup its exact fact IDs within the same block. Do not add, remove, or move facts, change claim atoms, rewrite provenance, or introduce paraphrase-distance/lexical-overlap rejection rules. A fluent faithful paraphrase is allowed; factual novelty or a high-risk unsupported detail should be fixed.\n"
            "Outside explicitly authorized recomposition, you may combine items only through an exact entry in that block's allowed_merges list. Return the exact merge_id and exact source_item_ids in the supplied order. A grant may contain two items/one edge or 3+ items connected by the listed SAME_FACT relation graph. Do not invent, remove, or change edges or items. The merged text must preserve the union of the source items' already-supported material and add no facts.\n"
            "Write connected, subject-first local-news prose. Establish attribution for each connected community-report cluster, then keep it in scope instead of repeating 'житель сообщает' before every clause. State the development directly; avoid message-by-message narration ('в одном из сообщений', 'другое сообщение описывает', 'опубликовано объявление о'). Preserve disagreement and unknown location/time honestly. Do not invent a chronology or street-level contrast to explain differing reports. An advertised route is a stated offer: phrase it as advertised/announced destinations without claiming actual operation or appending a generic disclaimer about verification. A short label or empty headline is preferable to a thesis repeated in the body. Keep every distinct supported microdetail.\n"
            "A rubric does not imply geographic proximity. Keep each named place attached to its own observation; never infer a shared district, relative distance, cause, city-wide condition, or routine state.\n"
            f"The final digest text should fit within {max_chars} characters where possible without dropping material facts.\n"
            'Return only JSON: {"blocks":[{"block_id":"...","items":[{"item_id":"...","headline":"...","body":"...","emoji":"..."}],"merges":[{"merge_id":"...","source_item_ids":["exact IDs from grant"],"headline":"...","body":"...","emoji":"..."}]}]}.\n'
            "Omit unchanged items. Keep every block present and in its original order.\n"
            + ("Requested validation issues:\n" + "\n".join(repair_lines) if repair_lines else "")
        )
        if recompose_ids:
            system_prompt += (
                "\nFor every block explicitly marked allow_recomposition, you MUST return "
                "recomposed_items and leave items/merges empty. This is presentation regrouping, "
                "not fact deletion: represent every fact from items marked "
                "targeted_for_recomposition exactly once. Do not include facts from other items; "
                "use target_recomposition_fact_ids from the user input as an exact checklist and "
                "copy every ID into exactly one replacement item's covered_fact_ids. "
                "The program restores non-target fact items byte-for-byte. Keep standalone "
                "summary-only items unchanged. If a targeted fact item also carries summary-only "
                "units, preserve each such unit exactly once: include its unit ID in one replacement "
                "item's composition_unit_ids and include a matching summary-only claim with that "
                "unit ID. Do not add summary units from non-target items. "
                "Combine related reports into readable paragraphs of roughly "
                "250–500 characters where the evidence permits. These are readability targets, not quotas. If a connected "
                "service story is longer, split it into two or three narrative groups by place or "
                "time period, not one item per street. Avoid a giant street-by-street paragraph. "
                "Do not narrate source messages: avoid phrases such as 'в одном из сообщений', "
                "'также сообщалось', 'сообщается в городе', 'in a separate message', "
                "'it was also reported', or 'one message said'. State the supported city situation "
                "directly and use natural attribution once for each report cluster, such as 'по "
                "сообщениям жителей'. Keep common details "
                "once while retaining each distinct duration, location, observation time and uncertainty. "
                "Do not invent geography or connective causes. Each replacement item has "
                "covered_fact_ids, headline (short label or empty), body and optional emoji. "
                "The program derives fact-bearing unit membership and Claim Atoms from exact frozen "
                "facts; omit fact claims and Story/support IDs. Only when preserving a targeted "
                "summary-only unit, supply its composition_unit_ids and claims "
                "[{text, covered_fact_ids: [], summary_unit_ids}] grounded in that unit. "
                "Do not mimic Claim Atoms as prose. Preserve all unique detail. "
                "In reports of bus prices distinguish the destination paid for from the final "
                "destination of a passing bus; never turn the latter into the fare destination. "
                "Use natural attribution such as 'по сообщениям жителей', not descriptions of chats. "
                f"During a recomposition batch, include each authorized block in recomposed_items and leave its items/merges empty. You may omit untouched blocks; the program preserves them byte-for-byte. Aim for {MAX_POWER_REPORT_ITEMS_PER_BLOCK} cohesive power-report items with a clear subject and an evidence-supported relation. More developed or synthesized passages are allowed when needed to preserve the facts; do not leave multiple short isolated single-observation items or turn each street or Story into its own paragraph. The program restores only non-target items. If a targeted item also contains water, heating or another service fact, that exact fact remains required in your replacement; it may receive its own service paragraph. Do not repeat the same polyclinic or district observation in different items."
            )
        target_recomposition_fact_ids = sorted(
            {
                str(fact_id)
                for block in draft.blocks
                for item in block.items
                if item.item_id in recompose_target_ids
                for fact_id in item.covered_fact_ids
            }
        )
        target_recomposition_summary_unit_ids = sorted(
            {
                str(unit_id)
                for block in draft.blocks
                if block.block_id in recompose_ids
                for item in block.items
                if item.item_id in recompose_target_ids
                for unit_id in (
                    item.composition_unit_ids
                    or ((item.composition_unit_id,) if item.composition_unit_id else ())
                )
                if any(
                    str(unit.unit_id) == str(unit_id) and not unit.fact_ids
                    for unit in plan_blocks[block.block_id].composition_units
                )
            }
        )
        user_prompt = json.dumps(
            {
                "target_item_ids": sorted(target_ids),
                "target_recomposition_fact_ids": target_recomposition_fact_ids,
                "target_recomposition_summary_unit_ids": target_recomposition_summary_unit_ids,
                "blocks": editor_blocks,
            },
            ensure_ascii=False,
            indent=2,
        )
        chat_kwargs: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "reasoning_effort": "none",
            "thinking": False,
            "max_tokens": 4096,
        }
        if model:
            chat_kwargs["model"] = model
        try:
            raw_response = (await provider.chat_completion(**chat_kwargs) or "").strip()
            json_match = _JSON_BLOCK_RE.search(raw_response)
            if json_match:
                parsed = json.loads(json_match.group(1))
            else:
                first_brace, last_brace = raw_response.find("{"), raw_response.rfind("}")
                if first_brace < 0 or last_brace <= first_brace:
                    raise ValueError("editor response did not contain a JSON object")
                parsed = json.loads(raw_response[first_brace : last_brace + 1])
            raw_blocks = parsed.get("blocks") if isinstance(parsed, Mapping) else None
            if not isinstance(raw_blocks, list):
                raise ValueError("editor response blocks must be a list")
            expected_block_ids = [block.block_id for block in draft.blocks]
            received_ids = [
                str(block.get("block_id", "")) if isinstance(block, Mapping) else ""
                for block in raw_blocks
            ]
            if recompose_ids:
                raw_by_block_id: dict[str, Mapping[str, Any]] = {}
                for raw_block in raw_blocks:
                    if not isinstance(raw_block, Mapping):
                        raise ValueError("editor block must be an object")
                    block_id = str(raw_block.get("block_id", ""))
                    if block_id not in expected_block_ids:
                        raise ValueError("editor added an unknown block")
                    if block_id in raw_by_block_id:
                        raise ValueError("editor returned a block more than once")
                    raw_by_block_id[block_id] = raw_block
                if not recompose_ids.issubset(raw_by_block_id):
                    raise ValueError("editor omitted an authorized recomposition block")
                # Recomposition changes only explicitly authorized blocks. Reorder
                # returned blocks deterministically and restore omitted untouched
                # blocks from the frozen input without applying model-authored edits.
                raw_blocks = [
                    raw_by_block_id.get(block_id, {"block_id": block_id, "items": [], "merges": []})
                    for block_id in expected_block_ids
                ]
            elif received_ids != expected_block_ids:
                raise ValueError("editor changed, omitted, or reordered blocks")

            recomposed: dict[str, list[Any]] = {}
            for raw_block in raw_blocks:
                if "recomposed_items" not in raw_block:
                    continue
                block_id = str(raw_block["block_id"])
                if (
                    block_id not in recompose_ids
                    or raw_block.get("items")
                    or raw_block.get("merges")
                ):
                    raise ValueError("unapproved or mixed recomposition")
                if not isinstance(raw_block["recomposed_items"], list):
                    raise ValueError("recomposed_items must be a list")
                recomposed[block_id] = raw_block["recomposed_items"]
            if recompose_ids and set(recomposed) != recompose_ids:
                missing_blocks = sorted(recompose_ids - set(recomposed))
                raise DigestRecompositionError(
                    "recomposition omitted authorized blocks: " + ", ".join(missing_blocks)
                )
            if recomposed:
                # Reuse the production parser: exact membership and evidence ownership,
                # not model-authored provenance. Untouched blocks remain byte-for-byte intact.
                parser_blocks = []
                for block in draft.blocks:
                    items = recomposed.get(block.block_id)
                    if items is not None:
                        plan_block = plan_blocks[block.block_id]
                        unit_by_id = {
                            str(unit.unit_id): unit for unit in plan_block.composition_units
                        }
                        target_fact_ids = {
                            str(fact_id)
                            for item in block.items
                            if item.item_id in recompose_target_ids
                            for fact_id in item.covered_fact_ids
                        }
                        returned_fact_ids = [
                            str(fact_id)
                            for raw_item in items
                            if isinstance(raw_item, Mapping)
                            and isinstance(raw_item.get("covered_fact_ids"), list)
                            for fact_id in raw_item["covered_fact_ids"]
                        ]
                        if (
                            len(returned_fact_ids) != len(set(returned_fact_ids))
                            or set(returned_fact_ids) != target_fact_ids
                        ):
                            duplicate_fact_ids = sorted(
                                {
                                    fact_id
                                    for fact_id in returned_fact_ids
                                    if returned_fact_ids.count(fact_id) > 1
                                }
                            )
                            missing_fact_ids = sorted(target_fact_ids - set(returned_fact_ids))
                            extra_fact_ids = sorted(set(returned_fact_ids) - target_fact_ids)
                            details = []
                            if missing_fact_ids:
                                details.append("missing facts: " + ", ".join(missing_fact_ids))
                            if extra_fact_ids:
                                details.append("extra facts: " + ", ".join(extra_fact_ids))
                            if duplicate_fact_ids:
                                details.append("duplicate facts: " + ", ".join(duplicate_fact_ids))
                            raise DigestRecompositionError(
                                "recomposition fact partition mismatch (" + "; ".join(details) + ")"
                            )
                        target_summary_unit_ids = {
                            str(unit_id)
                            for item in block.items
                            if item.item_id in recompose_target_ids
                            for unit_id in (
                                item.composition_unit_ids
                                or ((item.composition_unit_id,) if item.composition_unit_id else ())
                            )
                            if unit_id in unit_by_id and not unit_by_id[unit_id].fact_ids
                        }
                        returned_summary_unit_ids = [
                            str(unit_id)
                            for raw_item in items
                            if isinstance(raw_item, Mapping)
                            for unit_id in raw_item.get("composition_unit_ids", [])
                            if str(unit_id) in target_summary_unit_ids
                        ]
                        all_returned_summary_unit_ids = [
                            str(unit_id)
                            for raw_item in items
                            if isinstance(raw_item, Mapping)
                            for unit_id in raw_item.get("composition_unit_ids", [])
                            if str(unit_id) in unit_by_id and not unit_by_id[str(unit_id)].fact_ids
                        ]
                        if (
                            len(returned_summary_unit_ids) != len(set(returned_summary_unit_ids))
                            or set(returned_summary_unit_ids) != target_summary_unit_ids
                            or set(all_returned_summary_unit_ids) != target_summary_unit_ids
                        ):
                            raise ValueError(
                                "recomposition must preserve only its targeted summary units exactly once"
                            )
                        fact_text_by_id = {
                            str(fact.fact_id): str(fact.text) for fact in plan_block.required_facts
                        }
                        fact_unit_by_id = {
                            str(fact_id): str(unit.unit_id)
                            for unit in plan_block.composition_units
                            for fact_id in unit.fact_ids
                        }
                        normalized_items: list[Any] = []
                        for raw_item in items:
                            if not isinstance(raw_item, Mapping):
                                raise ValueError("recomposed item must be an object")
                            fact_ids = raw_item.get("covered_fact_ids")
                            if not isinstance(fact_ids, list) or not isinstance(
                                raw_item.get("claims", []), list
                            ):
                                raise ValueError(
                                    "recomposed facts and supplied summary claims must be lists"
                                )
                            normalized = dict(raw_item)
                            # Unit membership is determined from exact fact IDs, never from
                            # model-authored provenance. The parser still verifies this map.
                            replacement_summary_unit_ids = [
                                str(unit_id)
                                for unit_id in raw_item.get("composition_unit_ids", [])
                                if str(unit_id) in target_summary_unit_ids
                            ]
                            normalized["composition_unit_ids"] = list(
                                dict.fromkeys(
                                    [
                                        fact_unit_by_id[str(fact_id)]
                                        for fact_id in fact_ids
                                        if str(fact_id) in fact_unit_by_id
                                    ]
                                    + replacement_summary_unit_ids
                                )
                            )
                            # Claim Atoms are fixed-evidence metadata, not model-authored
                            # paraphrases. Visible prose is separately checked against these
                            # exact facts by the normal Evidence Boundary validator.
                            fact_claims = [
                                {
                                    "text": fact_text_by_id[str(fact_id)],
                                    "covered_fact_ids": [str(fact_id)],
                                    "summary_unit_ids": [],
                                }
                                for fact_id in fact_ids
                                if str(fact_id) in fact_text_by_id
                            ]
                            if len(fact_claims) != len(fact_ids):
                                raise ValueError("recomposed item references an unknown fact")
                            summary_claims = []
                            for raw_claim in raw_item.get("claims", []):
                                if not isinstance(raw_claim, Mapping):
                                    raise ValueError("recomposed claim must be an object")
                                summary_ids = raw_claim.get("summary_unit_ids", [])
                                if not summary_ids:
                                    continue
                                claim_fact_ids = raw_claim.get("covered_fact_ids", [])
                                if claim_fact_ids:
                                    raise ValueError(
                                        "a recomposed claim cannot mix facts and summary units"
                                    )
                                if any(
                                    str(summary_id) not in target_summary_unit_ids
                                    for summary_id in summary_ids
                                ):
                                    raise ValueError(
                                        "recomposed claim references a non-target summary unit"
                                    )
                                summary_claims.append(
                                    {
                                        "text": str(raw_claim.get("text", "")),
                                        "covered_fact_ids": [],
                                        "summary_unit_ids": [
                                            str(summary_id) for summary_id in summary_ids
                                        ],
                                    }
                                )
                            normalized["claims"] = fact_claims + summary_claims
                            normalized_items.append(normalized)
                        items = normalized_items
                        for item in block.items:
                            item_unit_ids = item.composition_unit_ids or (
                                (item.composition_unit_id,) if item.composition_unit_id else ()
                            )
                            if (
                                item.item_id not in recompose_target_ids
                                or not item.covered_fact_ids
                            ):
                                items.append(
                                    {
                                        "composition_unit_ids": list(item_unit_ids),
                                        "covered_fact_ids": list(item.covered_fact_ids),
                                        "headline": item.headline,
                                        "body": item.body,
                                        "emoji": item.emoji,
                                        "claims": [
                                            {
                                                "text": claim.text,
                                                "covered_fact_ids": list(claim.covered_fact_ids),
                                                "summary_unit_ids": list(claim.summary_unit_ids),
                                            }
                                            for claim in item.claims
                                        ],
                                    }
                                )
                    if items is None:
                        items = [
                            {
                                "composition_unit_ids": list(
                                    item.composition_unit_ids
                                    or (
                                        (item.composition_unit_id,)
                                        if item.composition_unit_id
                                        else ()
                                    )
                                ),
                                "covered_fact_ids": list(item.covered_fact_ids),
                                "headline": item.headline,
                                "body": item.body,
                                "emoji": item.emoji,
                                "claims": [
                                    {
                                        "text": claim.text,
                                        "covered_fact_ids": list(claim.covered_fact_ids),
                                        "summary_unit_ids": list(claim.summary_unit_ids),
                                    }
                                    for claim in item.claims
                                ],
                            }
                            for item in block.items
                        ]
                    parser_blocks.append({"block_id": block.block_id, "items": items})
                checked = _parse_composition_writer_output({"blocks": parser_blocks}, plan=plan)
                # Do not mix legacy patches with structural replacements in one batch.
                if any(b.get("items") or b.get("merges") for b in raw_blocks):
                    raise ValueError("recomposition batch cannot include text patches")
                return replace(
                    draft,
                    blocks=tuple(
                        new if old.block_id in recomposed else old
                        for old, new in zip(draft.blocks, checked.blocks, strict=True)
                    ),
                )

            updates: dict[str, Mapping[str, Any]] = {}
            merges: dict[str, tuple[tuple[str, ...], Mapping[str, Any]]] = {}
            for raw_block in raw_blocks:
                if not isinstance(raw_block, Mapping):
                    raise ValueError("editor block must be an object")
                block_id = str(raw_block["block_id"])
                if not isinstance(raw_block.get("items", []), list) or not isinstance(
                    raw_block.get("merges", []), list
                ):
                    raise ValueError("editor items and merges must be lists")
                for update in raw_block.get("items", []):
                    if not isinstance(update, Mapping):
                        raise ValueError("editor item patch must be an object")
                    item_id = str(update.get("item_id", ""))
                    if (
                        item_id not in item_locations
                        or item_locations[item_id][0].block_id != block_id
                    ):
                        raise ValueError(f"editor changed or invented item ID: {item_id}")
                    if item_id in updates:
                        raise ValueError(f"duplicate editor patch for {item_id}")
                    if item_id not in target_ids:
                        raise ValueError(
                            f"editor changed an item outside the target set: {item_id}"
                        )
                    updates[item_id] = update
                for merge in raw_block.get("merges", []):
                    if not isinstance(merge, Mapping):
                        raise ValueError("editor merge must be an object")
                    merge_id = str(merge.get("merge_id", ""))
                    input_ids = tuple(str(value) for value in merge.get("source_item_ids", []))
                    approved = next(
                        (
                            value
                            for value in approved_by_block.get(block_id, [])
                            if value["merge_id"] == merge_id
                        ),
                        None,
                    )
                    if approved is None or input_ids != tuple(approved["source_item_ids"]):
                        raise ValueError(f"editor proposed unapproved/widened merge {merge_id}")
                    if merge_id in merges:
                        raise ValueError(f"duplicate merge {merge_id}")
                    if any(item_id in updates for item_id in input_ids):
                        raise ValueError(
                            "merged input items cannot also have independent text patches"
                        )
                    if any(item_id not in target_ids for item_id in input_ids):
                        raise ValueError("merge input is outside the targeted repair set")
                    merges[merge_id] = (input_ids, merge)

            used_merge_inputs = {
                item_id for input_ids, _merge in merges.values() for item_id in input_ids
            }
            if len(used_merge_inputs) != sum(
                len(input_ids) for input_ids, _merge in merges.values()
            ):
                raise ValueError("an input item was consumed by more than one merge")

            revised_blocks: list[DigestNarrativeBlockDraft] = []
            for block in draft.blocks:
                revised_items: list[DigestEditorialItemDraft] = []
                for item in block.items:
                    if item.item_id in used_merge_inputs:
                        continue
                    patch = updates.get(item.item_id)
                    if patch is None:
                        revised_items.append(item)
                        continue
                    headline = patch.get("headline", item.headline)
                    body = patch.get("body", item.body)
                    emoji = patch.get("emoji", item.emoji)
                    if not all(isinstance(value, str) for value in (headline, body, emoji)):
                        raise ValueError(
                            f"editor text patch fields must be strings for {item.item_id}"
                        )
                    revised_items.append(
                        replace(
                            item,
                            headline=headline.strip(),
                            body=body.strip(),
                            emoji=emoji.strip(),
                        )
                    )
                for merge_id, (input_ids, merge) in merges.items():
                    if item_locations[input_ids[0]][0].block_id != block.block_id:
                        continue
                    input_items = [item_locations[item_id][1] for item_id in input_ids]
                    if not all(isinstance(merge.get(key), str) for key in ("headline", "body")):
                        raise ValueError(f"merged text missing headline/body: {merge_id}")
                    unit_ids = tuple(
                        dict.fromkeys(
                            unit_id
                            for item in input_items
                            for unit_id in (
                                item.composition_unit_ids
                                or ((item.composition_unit_id,) if item.composition_unit_id else ())
                            )
                        )
                    )
                    merged_fact_ids = tuple(
                        dict.fromkeys(fid for item in input_items for fid in item.covered_fact_ids)
                    )
                    stories = tuple(
                        dict.fromkeys(sid for item in input_items for sid in item.covered_story_ids)
                    )
                    supports = tuple(
                        dict.fromkeys(sid for item in input_items for sid in item.cited_support_ids)
                    )
                    claims = tuple(
                        dict.fromkeys(claim for item in input_items for claim in item.claims)
                    )
                    revised_items.append(
                        DigestEditorialItemDraft(
                            headline=str(merge["headline"]).strip(),
                            body=str(merge["body"]).strip(),
                            covered_story_ids=stories,
                            cited_support_ids=supports,
                            claims=claims,
                            emoji=str(merge.get("emoji") or input_items[0].emoji).strip(),
                            item_id=f"item:{merge_id}",
                            composition_unit_id=unit_ids[0] if len(unit_ids) == 1 else "",
                            covered_fact_ids=merged_fact_ids,
                            composition_unit_ids=unit_ids,
                            source_item_ids=input_ids,
                            source_item_fact_ids=tuple(
                                (source_item.item_id, tuple(source_item.covered_fact_ids))
                                for source_item in input_items
                            ),
                            composition_merge_id=merge_id,
                        )
                    )
                revised_blocks.append(
                    DigestNarrativeBlockDraft(block_id=block.block_id, items=tuple(revised_items))
                )
            return sanitize_digest_narrative_draft(
                DigestNarrativeDraft(
                    blocks=tuple(revised_blocks), situation_items=draft.situation_items
                )
            )
        except Exception as exc:
            logger.warning(
                "DigestEditor composition repair rejected (%s: %s); returning original draft",
                type(exc).__name__,
                exc,
            )
            if recompose_ids and isinstance(exc, DigestRecompositionError):
                raise
            return draft

    async def polish_and_compress(
        self,
        draft: DigestNarrativeDraft,
        *,
        plan: Any = None,
        evidence: Mapping[str, PublicationEvidence] | None = None,
        max_chars: int = 3600,
        model: str | None = None,
        violations: Sequence[str] | None = None,
        target_item_ids: Sequence[str] | None = None,
        allowed_merges: Mapping[str, Any] | None = None,
        recompose_block_ids: Sequence[str] = (),
    ) -> DigestNarrativeDraft:
        """Apply targeted journalistic polish, contrast synthesis, and length compression."""
        if self._provider is None:
            logger.warning("No AI provider available for DigestEditor; returning original draft")
            return draft

        if plan is not None and any(
            getattr(block, "composition_units", ()) for block in getattr(plan, "blocks", ())
        ):
            return await self._polish_composition(
                draft,
                plan=plan,
                evidence=evidence or {},
                max_chars=max_chars,
                model=model,
                violations=violations,
                target_item_ids=target_item_ids,
                allowed_merges=allowed_merges or {},
                recompose_block_ids=recompose_block_ids,
            )

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
                "- Correct unsupported details, over-specification, or an upgrade in certainty. Preserve an eligible single-source/unconfirmed PUBLISH community report as a faithfully attributed report; lack of corroboration is not a defect.\n"
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
            "   - Synthesize a localized contrast only when the reports refer to the same named area. Keep each fact attached to its own cited location; state conditions from different neighborhoods in separate short sentences. Never use one area's name as an umbrella for other locations or infer a shared neighborhood from terrain or elevation.\n"
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
