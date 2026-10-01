"""Canonical policy for reader-quality findings in Event-First articles.

Evidence Boundary findings are intentionally absent from this registry; they
remain governed by ``article_validator``.  A reader-quality code that is not
registered here is a configuration error and must never be treated as safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

ArticleQualitySeverity = Literal["repair", "warning", "blocking"]
ArticleQualityRepairScope = Literal[
    "unit",
    "support_units",
    "story_unit",
    "whole_draft",
    "none",
]
ArticleQualityPublicationEffect = Literal[
    "repair_recommended",
    "block_publication",
    "readiness_incomplete",
    "diagnostic_only",
]


class ArticleQualityPolicyError(ValueError):
    """Raised when a reader-quality finding has no valid shared policy."""


@dataclass(frozen=True)
class ArticleQualityFindingPolicy:
    """Reader-facing response assigned to one deterministic quality code."""

    finding_class: str
    severity: ArticleQualitySeverity
    repair_scope: ArticleQualityRepairScope
    publication_effect: ArticleQualityPublicationEffect
    description: str


_POLICIES: dict[str, ArticleQualityFindingPolicy] = {
    "CONTRADICTORY_SERVICE_STATE": ArticleQualityFindingPolicy(
        "service_consistency",
        "blocking",
        "unit",
        "block_publication",
        "Opposing service states need an explicit localized contrast.",
    ),
    "THEME_MISMATCHED_SECTION": ArticleQualityFindingPolicy(
        "theme_placement",
        "repair",
        "support_units",
        "repair_recommended",
        "A paragraph cites support whose known theme does not fit the section.",
    ),
    "UNCLASSIFIED_STORY_IN_CONNECTIVITY_SECTION": ArticleQualityFindingPolicy(
        "theme_placement",
        "repair",
        "support_units",
        "repair_recommended",
        "A paragraph in the connectivity section has no known thematic link.",
    ),
    "DUPLICATE_ARTICLE_HEADING": ArticleQualityFindingPolicy(
        "article_structure",
        "repair",
        "unit",
        "repair_recommended",
        "A heading repeats the title or another section heading.",
    ),
    "UNDEVELOPED_LEAD_PROMISE": ArticleQualityFindingPolicy(
        "lead_alignment",
        "repair",
        "unit",
        "repair_recommended",
        "The lead promises a key storyline that the article does not develop.",
    ),
    "ARTICLE_INVENTORY_RHYTHM": ArticleQualityFindingPolicy(
        "paragraph_rhythm",
        "repair",
        "unit",
        "repair_recommended",
        "A run of one-sentence paragraphs from separate Stories prompts review but does not prove an inventory.",
    ),
    "QUOTE_ROLL_PARAGRAPH": ArticleQualityFindingPolicy(
        "quote_count_heuristic",
        "repair",
        "unit",
        "repair_recommended",
        "A paragraph contains more than two quote spans; review whether speech should be synthesized.",
    ),
    "CONSECUTIVE_DIRECT_SPEECH_ROLL": ArticleQualityFindingPolicy(
        "direct_speech_structure",
        "blocking",
        "unit",
        "block_publication",
        "Consecutive direct-speech spans are joined only by list punctuation.",
    ),
    "ARTICLE_PLACE_AREA_MISMATCH": ArticleQualityFindingPolicy(
        "geography_consistency",
        "blocking",
        "unit",
        "block_publication",
        "A paragraph assigns a place or street to the wrong area.",
    ),
    "ARTICLE_AREA_BEFORE_STREET_ORDER": ArticleQualityFindingPolicy(
        "geography_precision",
        "repair",
        "unit",
        "repair_recommended",
        "A paragraph introduces a street before its area.",
    ),
    "PRIVATE_SECTOR_AREA_UNSPECIFIED": ArticleQualityFindingPolicy(
        "geography_precision",
        "repair",
        "unit",
        "repair_recommended",
        "Clarify that the source did not name a district; do not infer one.",
    ),
    "UNQUOTED_COMMERCIAL_PROVIDER_NAME": ArticleQualityFindingPolicy(
        "name_typography",
        "repair",
        "unit",
        "repair_recommended",
        "A supported provider name needs typographic quotation marks.",
    ),
    "INCOMPLETE_QUANTITY_PHRASE": ArticleQualityFindingPolicy(
        "sentence_completeness",
        "repair",
        "unit",
        "repair_recommended",
        "A trailing quantity heuristic suggests checking for an omitted unit or counted noun.",
    ),
    "OVERLOADED_ROSTER_PARAGRAPH": ArticleQualityFindingPolicy(
        "structural_service_roster",
        "blocking",
        "unit",
        "block_publication",
        "A multi-place service sentence lists source-backed states without a narrative relation.",
    ),
    "MULTI_SENTENCE_ADDRESS_STATUS_ROSTER": ArticleQualityFindingPolicy(
        "structural_service_roster",
        "blocking",
        "support_units",
        "block_publication",
        "Several same-service place states appear in separate sentences without narrative relation.",
    ),
    "DIRECTORY_TIMETABLE_SECTION": ArticleQualityFindingPolicy(
        "structural_directory_dominance",
        "blocking",
        "support_units",
        "block_publication",
        "Source-backed routine listings dominate the section rather than supporting a city-life narrative.",
    ),
    "CROSS_SECTION_REPETITION": ArticleQualityFindingPolicy(
        "repetition",
        "repair",
        "unit",
        "repair_recommended",
        "A supported fact repeats in another section without a new detail.",
    ),
    "REPEATED_CENTRAL_THESIS": ArticleQualityFindingPolicy(
        "repetition",
        "repair",
        "support_units",
        "repair_recommended",
        "The article repeats its central point without new development.",
    ),
    "MISSING_DEVELOP_STORY": ArticleQualityFindingPolicy(
        "coverage_readiness",
        "repair",
        "story_unit",
        "readiness_incomplete",
        "A key planned storyline is missing from the article.",
    ),
    "MISSING_DETAIL_SUPPORT": ArticleQualityFindingPolicy(
        "detail_retention",
        "repair",
        "unit",
        "repair_recommended",
        "A planned concrete detail is absent from an otherwise represented story.",
    ),
}

ARTICLE_QUALITY_FINDING_POLICIES: Mapping[str, ArticleQualityFindingPolicy] = MappingProxyType(
    _POLICIES
)


def article_quality_policy(code: str) -> ArticleQualityFindingPolicy:
    """Return the policy for ``code`` or raise instead of silently allowing it."""
    try:
        return ARTICLE_QUALITY_FINDING_POLICIES[code]
    except KeyError as exc:
        raise ArticleQualityPolicyError(
            f"Reader-quality finding code has no policy: {code!r}"
        ) from exc


def validate_article_quality_severity(code: str, severity: str | None) -> str:
    """Validate a supplied severity and return the canonical severity."""
    policy = article_quality_policy(code)
    if severity is not None and severity != policy.severity:
        raise ArticleQualityPolicyError(
            f"Reader-quality finding {code!r} has severity {severity!r}; "
            f"policy requires {policy.severity!r}"
        )
    return policy.severity


def article_quality_policy_metadata(code: str) -> dict[str, str]:
    """Return non-prose policy attributes for preview and publication metadata."""
    policy = article_quality_policy(code)
    return {
        "finding_class": policy.finding_class,
        "severity": policy.severity,
        "repair_scope": policy.repair_scope,
        "publication_effect": policy.publication_effect,
    }


ARTICLE_WHOLE_DRAFT_FINDING_CODES = frozenset(
    code
    for code, policy in ARTICLE_QUALITY_FINDING_POLICIES.items()
    if policy.repair_scope in {"support_units", "whole_draft"}
)
