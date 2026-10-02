"""Validated writer-facing input assembled from a frozen article snapshot."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from src.publication.article_composition import ArticleCompositionPlan
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_material import ArticleMaterialProjection
from src.publication.article_writer_context import (
    _ARTICLE_EVIDENCE_BEGIN,
    _ARTICLE_EVIDENCE_END,
    _ARTICLE_QUOTE_BEGIN,
    _ARTICLE_QUOTE_END,
    ARTICLE_WRITER_CONTEXT_VERSION,
    expected_article_writer_support_ids,
    render_article_writer_context_with_stats,
)
from src.publication.narrative_contract import ARTICLE_NARRATIVE_PROMPT_VERSION


@dataclass(frozen=True)
class ArticleWriterInput:
    """Complete, bounded writer request material and its safe accounting."""

    context_text: str
    expected_support_ids: tuple[str, ...]
    exposed_support_ids: tuple[str, ...]
    quote_allowlist: tuple[str, ...]
    metadata: dict[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_support_ids", tuple(self.expected_support_ids))
        object.__setattr__(self, "exposed_support_ids", tuple(self.exposed_support_ids))
        object.__setattr__(self, "quote_allowlist", tuple(self.quote_allowlist))
        object.__setattr__(self, "metadata", dict(self.metadata))


def _extract_exposed_support_ids(context_text: str) -> tuple[tuple[str, ...], int]:
    if (
        context_text.count(_ARTICLE_EVIDENCE_BEGIN) != 1
        or context_text.count(_ARTICLE_EVIDENCE_END) != 1
    ):
        raise ValueError("article writer context must contain exactly one evidence inventory")
    start = context_text.index(_ARTICLE_EVIDENCE_BEGIN) + len(_ARTICLE_EVIDENCE_BEGIN)
    end = context_text.index(_ARTICLE_EVIDENCE_END, start)
    if end < start:
        raise ValueError("article writer evidence inventory markers are out of order")

    support_ids: list[str] = []
    record_count = 0
    inventory = context_text[start:end].strip()
    for line_number, line in enumerate(inventory.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"article writer evidence inventory record {line_number} is invalid JSON"
            ) from exc
        if not isinstance(record, dict) or record.get("record_type", record.get("t")) != "support":
            raise ValueError(
                f"article writer evidence inventory record {line_number} is not a support"
            )
        ids = record.get("support_ids", record.get("i"))
        if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
            raise ValueError(
                f"article writer evidence inventory record {line_number} has invalid support IDs"
            )
        record_count += 1
        support_ids.extend(ids)
    if len(support_ids) != len(set(support_ids)):
        raise ValueError("article writer evidence inventory exposes a support more than once")
    return tuple(support_ids), record_count


def _extract_quote_allowlist(context_text: str) -> tuple[str, ...]:
    if context_text.count(_ARTICLE_QUOTE_BEGIN) != 1 or context_text.count(_ARTICLE_QUOTE_END) != 1:
        raise ValueError("article writer context must contain exactly one quote allowlist")
    start = context_text.index(_ARTICLE_QUOTE_BEGIN) + len(_ARTICLE_QUOTE_BEGIN)
    end = context_text.index(_ARTICLE_QUOTE_END, start)
    if end < start:
        raise ValueError("article writer quote allowlist markers are out of order")

    candidates: list[str] = []
    for line_number, line in enumerate(context_text[start:end].splitlines(), start=1):
        value = line.strip()
        if not value or value.startswith("Each following JSON string"):
            continue
        try:
            candidate = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"article writer quote candidate {line_number} is invalid JSON"
            ) from exc
        if not isinstance(candidate, str):
            raise ValueError(f"article writer quote candidate {line_number} is not a string")
        if candidate.startswith("(none;"):
            continue
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _hash_support_ids(support_ids: tuple[str, ...]) -> str:
    payload = json.dumps(support_ids, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_article_writer_input(
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan,
    *,
    material_projection: ArticleMaterialProjection,
    composition_plan: ArticleCompositionPlan,
) -> ArticleWriterInput:
    """Build and validate the complete writer dossier from frozen article inputs."""
    expected_support_ids = expected_article_writer_support_ids(
        context,
        coverage_plan,
        material_projection=material_projection,
        composition_plan=composition_plan,
    )
    context_text, stats = render_article_writer_context_with_stats(
        context,
        coverage_plan,
        material_projection=material_projection,
        composition_plan=composition_plan,
        materialization_mode="packetized",
    )
    if stats is None:
        raise ValueError("article writer context did not return materialization statistics")

    exposed_support_ids, evidence_record_count = _extract_exposed_support_ids(context_text)
    if set(expected_support_ids) != set(exposed_support_ids):
        missing = sorted(set(expected_support_ids) - set(exposed_support_ids))
        unexpected = sorted(set(exposed_support_ids) - set(expected_support_ids))
        raise ValueError(
            "article writer context support accounting mismatch "
            f"(missing={missing!r}, unexpected={unexpected!r})"
        )
    quote_allowlist = _extract_quote_allowlist(context_text)

    metadata: dict[str, object] = {
        **stats.to_metadata(),
        "article_writer_context_version": ARTICLE_WRITER_CONTEXT_VERSION,
        "article_narrative_prompt_version": ARTICLE_NARRATIVE_PROMPT_VERSION,
        "context_character_count": len(context_text),
        "context_sha256": hashlib.sha256(context_text.encode("utf-8")).hexdigest(),
        "expected_support_count": len(expected_support_ids),
        "exposed_support_count": len(exposed_support_ids),
        "expected_support_ids_sha256": _hash_support_ids(expected_support_ids),
        "exposed_support_ids_sha256": _hash_support_ids(exposed_support_ids),
        "evidence_record_count": evidence_record_count,
        "quote_allowlist_count": len(quote_allowlist),
        "quote_allowlist_sha256": hashlib.sha256(
            json.dumps(quote_allowlist, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    return ArticleWriterInput(
        context_text=context_text,
        expected_support_ids=expected_support_ids,
        exposed_support_ids=exposed_support_ids,
        quote_allowlist=quote_allowlist,
        metadata=metadata,
    )
