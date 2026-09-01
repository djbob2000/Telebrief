"""Non-blocking prose quality diagnostics for narrative digests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.publication.digest_narrative import DigestNarrativeDraft
from src.publication.evidence import PublicationEvidence

DIGEST_DIAGNOSTICS_VERSION = "digest-diagnostics-v1"

_ATTRIBUTION_PATTERNS = [
    re.compile(
        r"\b(жители|жителей|горожане|горожан|очевидцы|очевидцев)\s+(сообщают|пишут|делятся|жалуются|отмечают)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bпо\s+(сообщениям|словам|информации|данным)\s+(жителей|горожан|очевидцев)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(в\s+соцсетях|в\s+местных\s+пабликах|в\s+сети|в\s+каналах)\s+(пишут|сообщают|появились)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bсообщают\s+(жители|горожане|очевидцы)\b", re.IGNORECASE),
]

_QUESTION_META_PATTERNS = [
    re.compile(
        r"\b(жители|горожане|жителей|горожан)\s+(интересуются|спрашивают|выясняют|узнают)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bвопрос\s+(о|об|про|по\s+поводу)\b", re.IGNORECASE),
    re.compile(r"\bпоступают\s+вопросы\b", re.IGNORECASE),
]


@dataclass(frozen=True)
class DigestQualityWarning:
    """A single diagnostic prose quality finding."""

    code: str
    message: str
    block_id: str | None = None
    item_index: int | None = None
    headline: str | None = None

    def as_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.block_id is not None:
            res["block_id"] = self.block_id
        if self.item_index is not None:
            res["item_index"] = self.item_index
        if self.headline is not None:
            res["headline"] = self.headline
        return res


@dataclass(frozen=True)
class DigestProseQualityAudit:
    """Audit record capturing all non-blocking prose diagnostics for a digest draft."""

    version: str = DIGEST_DIAGNOSTICS_VERSION
    warnings: tuple[DigestQualityWarning, ...] = ()
    compression_ratio: float = 1.0
    items_per_group: float = 1.0
    multi_story_item_count: int = 0
    single_story_item_count: int = 0
    dashboard_group_count: int = 0
    detail_item_count: int = 0

    @property
    def is_clean(self) -> bool:
        return len(self.warnings) == 0

    def as_metadata(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "is_clean": self.is_clean,
            "warning_count": len(self.warnings),
            "warnings": [w.as_dict() for w in self.warnings],
            "compression_ratio": self.compression_ratio,
            "items_per_group": self.items_per_group,
            "multi_story_item_count": self.multi_story_item_count,
            "single_story_item_count": self.single_story_item_count,
            "dashboard_group_count": self.dashboard_group_count,
            "detail_item_count": self.detail_item_count,
        }


def _has_attribution(text: str) -> bool:
    return any(p.search(text) for p in _ATTRIBUTION_PATTERNS)


def _check_duplicated_attribution(headline: str, body: str) -> bool:
    return _has_attribution(headline) and _has_attribution(body)


def _check_question_as_meta_news(
    headline: str,
    body: str,
    cited_evidences: Sequence[PublicationEvidence],
) -> bool:
    has_question_context = any(
        getattr(evi, "kind", "") == "resident_question"
        or getattr(evi, "framing", "") == "question_context"
        or getattr(evi, "publication_use", "") == "CONTEXT"
        for evi in cited_evidences
    )
    if not has_question_context:
        return False

    return any(p.search(headline) for p in _QUESTION_META_PATTERNS)


def _check_redundant_headline_in_body(headline: str, body: str) -> bool:
    norm_head = " ".join(headline.casefold().split()).strip(" .:,!-–—")
    norm_body = " ".join(body.casefold().split()).strip()
    if not norm_head or not norm_body:
        return False
    if norm_body.startswith(norm_head):
        return True
    first_sentence = re.split(r"[.!?]", norm_body)[0].strip()
    if first_sentence and first_sentence == norm_head:
        return True
    return False


def audit_digest_prose_quality(
    draft: DigestNarrativeDraft,
    evidence: Mapping[str, PublicationEvidence],
    presentation_plan: Any | None = None,
) -> DigestProseQualityAudit:
    """Run non-blocking diagnostics on a narrative digest draft."""
    warnings: list[DigestQualityWarning] = []

    detail_item_count = 0
    multi_story_item_count = 0
    single_story_item_count = 0
    covered_stories_detail = 0

    for block in draft.blocks:
        for idx, item in enumerate(block.items):
            detail_item_count += 1
            num_covered = len(item.covered_story_ids)
            covered_stories_detail += num_covered
            if num_covered > 1:
                multi_story_item_count += 1
            elif num_covered == 1:
                single_story_item_count += 1

            cited = [evidence[sid] for sid in item.cited_support_ids if sid in evidence]

            if _check_duplicated_attribution(item.headline, item.body):
                warnings.append(
                    DigestQualityWarning(
                        code="DUPLICATED_ATTRIBUTION",
                        message="Headline and body both contain conversational attribution phrases.",
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

            if _check_question_as_meta_news(item.headline, item.body, cited):
                warnings.append(
                    DigestQualityWarning(
                        code="QUESTION_AS_META_NEWS",
                        message="Headline frames a resident question as meta-news about resident inquiries.",
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

            if _check_redundant_headline_in_body(item.headline, item.body):
                warnings.append(
                    DigestQualityWarning(
                        code="REDUNDANT_HEADLINE_IN_BODY",
                        message="Body begins by repeating the headline verbatim.",
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

    dashboard_group_count = 0
    covered_stories_dash = 0
    if presentation_plan is not None:
        sit_plan = getattr(presentation_plan, "city_situation", presentation_plan)
        if sit_plan and getattr(sit_plan, "groups", None):
            groups = getattr(sit_plan, "groups", ())
            dashboard_group_count = len(groups)
            covered_stories_dash = sum(len(getattr(g, "covered_story_ids", ())) for g in groups)

    total_stories = covered_stories_detail + covered_stories_dash
    total_units = detail_item_count + dashboard_group_count
    compression_ratio = round(total_stories / total_units, 2) if total_units > 0 else 1.0
    items_per_group = round(detail_item_count / len(draft.blocks), 2) if draft.blocks else 0.0

    return DigestProseQualityAudit(
        version=DIGEST_DIAGNOSTICS_VERSION,
        warnings=tuple(warnings),
        compression_ratio=compression_ratio,
        items_per_group=items_per_group,
        multi_story_item_count=multi_story_item_count,
        single_story_item_count=single_story_item_count,
        dashboard_group_count=dashboard_group_count,
        detail_item_count=detail_item_count,
    )
