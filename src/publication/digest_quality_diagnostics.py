"""Prose quality diagnostics for narrative digests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from src.publication.digest_narrative import DigestNarrativeDraft
from src.publication.evidence import PublicationEvidence

DIGEST_DIAGNOSTICS_VERSION = "digest-diagnostics-v5"

_ATTRIBUTION_PATTERNS = [
    re.compile(
        r"\b(?:местн(?:ый|ая|ые)\s+)?(?:жител[ьи]|жительниц[аы]|горожан(?:ин|е)?|очевидц[ыа]|пользовател[ьи])\s+(?:сообща(?:ют|ет)|пиш(?:ут|ет)|дел(?:ятся|ится)|жалу(?:ются|ется)|отмеча(?:ют|ет)|уточня(?:ют|ет))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bпо\s+(?:сообщениям|словам|информации|данным)\s+(?:местн(?:ого|ых)\s+)?(?:жител(?:ей|я)|жительниц[ы]?|горожан(?:ина)?|очевидц(?:ев|а))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:в\s+соцсетях|в\s+местных\s+пабликах|в\s+сети|в\s+каналах)\s+(?:пишут|сообщают|появились)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bсообща(?:ют|ет)\s+(?:жител[ьи]|горожан(?:ин|е)?|очевидц[ыа])\b", re.IGNORECASE),
]

_QUESTION_META_PATTERNS = [
    re.compile(
        r"\b(жители|горожане|жителей|горожан)\s+(интересуются|спрашивают|выясняют|узнают)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bвопрос\s+(о|об|про|по\s+поводу)\b", re.IGNORECASE),
    re.compile(r"\bпоступают\s+вопросы\b", re.IGNORECASE),
]

_TECHNICAL_TOKEN_RE = re.compile(
    r"\b(?:story:\w+|frag:\w+|evidence:\w+|situation:\w+|block:\w+|AVAILABLE|UNAVAILABLE|STATUS_\w+|service_access)\b",
    re.IGNORECASE,
)
_TEMPORAL_CHAIN_RE = re.compile(
    r"(?:ранее\s+также|также\s+ранее|также\s+сообщается,\s+что\s+ранее)",
    re.IGNORECASE,
)
_REPETITIVE_BODY_ATTRIBUTION_RE = re.compile(
    r"(?:\bпо\s+(?:сообщениям|словам|информации|данным)\s+(?:жителей|горожан|очевидцев)\b|\b(?:жители|горожане|очевидцы)\s+(?:сообщают|пишут|делятся)\b)",
    re.IGNORECASE,
)
_CHAT_SLANG_OR_METADATA_RE = re.compile(
    r"(?:\b(?:чо\s+за\s+фигня|идите\s+нах|кинули\s+не\s+только\s+вас)\b|"
    r"\bсмайлик(?:ами|и)?\b|(?:публикуют\s+)?сообщения\s+с\s+эмодзи|"
    r"\bв\s+(?:городских\s+|местных\s+)?чатах\b|\bв\s+(?:городском\s+|местном\s+)?чате\b|"
    r"\bв\s+(?:(?:городском|местном|районном|локальном|телеграм[- ]?)\s+)?канале\b|"
    r"\bв\s+(?:телеграм[- ]каналах|telegram[- ]каналах|каналах|паблике|пабликах|соцсетях|социальных\s+сетях)\b|"
    r"\b(?:публикаци\w*|сообщени\w*|пост\w*)\s+(?:местного|городского|районного)\s+канала\b|"
    r"\bв\s+пабликах\b|\bучастники?\s+чата\b|\bперекличк[а-я]*\b)",
    re.IGNORECASE,
)
_POWER_REPORT_RE = re.compile(
    r"\b(?:свет\w*|электрич\w*|электроснабж\w*|энергоснабж\w*|обесточ\w*|напряжен\w*)\b",
    re.IGNORECASE,
)
_SOURCE_META_NARRATION_RE = re.compile(
    r"\b(?:в\s+(?:отдельном|другом|следующем|одном)\s+сообщени\w*|"
    r"(?:другое|отдельное)\s+сообщени\w*|"
    r"отдельно\s+(?:жители|горожане)\s+(?:сообща\w*|писа\w*)|"
    r"в\s+сообщениях\s+(?:упомина\w*|говор\w*|сообща\w*|отмеча\w*))\b",
    re.IGNORECASE,
)
_CLASSIFIED_AD_RE = re.compile(
    r"(?:\b(?:куплю|продам|купить\s+стекло|цена\s+от|позвонить\s+по\s+номеру)\b)",
    re.IGNORECASE,
)
_PROFANITY_AND_ABUSE_RE = re.compile(
    r"\b(?:"
    r"ху[йяеёюиы]\w*|"
    r"[нп]аху[йяе]\w*|"
    r"поху[йяе]\w*|"
    r"пизд\w*|"
    r"[её]б[аеёиуытлн]\w*|"
    r"у[её]б\w*|"
    r"в[ъь][её]б\w*|"
    r"за[её]б\w*|"
    r"от[её]б\w*|"
    r"раз[её]б\w*|"
    r"до[её]б\w*|"
    r"пере[её]б\w*|"
    r"бл[яя]т\w*|"
    r"бляд\w*|"
    r"сук[аиоуые]\w*|"
    r"мудак\w*|"
    r"мудил\w*|"
    r"залуп\w*|"
    r"срак\w*|"
    r"г[іие]вн\w*|"
    r"дерьм\w*|"
    r"хер\w*|"
    r"жоп\w*"
    r")\b",
    re.IGNORECASE,
)
_MALFORMED_CHAT_SYNTAX_RE = re.compile(
    r"(?:\(в\s+ответ\s+на\b|\(в\s+ответ\b|при\s+генератора\b)",
    re.IGNORECASE,
)
_GENERIC_HEADLINE_RE = re.compile(
    r"^(?:городские\s+события|события\s+в\s+городе|новости\s+города|городские\s+новости)$",
    re.IGNORECASE,
)
_BLOCKING_PROSE_CODES = frozenset(
    {
        "RAW_TECHNICAL_TOKEN",
        "CHAT_SLANG_OR_METADATA",
        "PROFANITY_OR_ABUSIVE_LANGUAGE",
        "MALFORMED_CHAT_SYNTAX",
    }
)


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
    """Audit record capturing prose diagnostics for a digest draft."""

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

    @property
    def is_publishable(self) -> bool:
        """Style warnings are advisory; only completed hard checks can block."""
        return not any(w.code in _BLOCKING_PROSE_CODES for w in self.warnings)

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


class DigestCheckStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - audit state, not a credential
    FAIL = "FAIL"  # noqa: S105 - audit state, not a credential
    NOT_EVALUATED = "NOT_EVALUATED"


@dataclass(frozen=True)
class DigestQualityCheck:
    """One explicit rendered-post audit result."""

    code: str
    status: DigestCheckStatus
    blocking: bool
    message: str
    text: str = ""
    evidence_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status.value,
            "blocking": self.blocking,
            "message": self.message,
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class DigestQualityAudit:
    """Whole-post audit with explicit blocking and unevaluated results."""

    checks: tuple[DigestQualityCheck, ...]
    prose_audit: DigestProseQualityAudit
    version: str = DIGEST_DIAGNOSTICS_VERSION

    @property
    def blocking_failures(self) -> tuple[DigestQualityCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if check.status == DigestCheckStatus.FAIL and check.blocking
        )

    @property
    def is_publishable(self) -> bool:
        return not self.blocking_failures

    @property
    def not_evaluated(self) -> tuple[DigestQualityCheck, ...]:
        return tuple(c for c in self.checks if c.status == DigestCheckStatus.NOT_EVALUATED)

    def as_metadata(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "is_publishable": self.is_publishable,
            "blocking_failure_count": len(self.blocking_failures),
            "not_evaluated_count": len(self.not_evaluated),
            "checks": [check.as_dict() for check in self.checks],
            "prose_observations": self.prose_audit.as_metadata(),
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
        power_item_indexes = [
            idx
            for idx, item in enumerate(block.items)
            if _POWER_REPORT_RE.search(f"{item.headline} {item.body}")
        ]
        if len(power_item_indexes) >= 3:
            warnings.append(
                DigestQualityWarning(
                    code="FRAGMENTED_SERVICE_REPORTS",
                    message=(
                        "Power observations are scattered across too many separate items. "
                        "Weave related locations and timelines into a few readable passages "
                        "while preserving every distinct report."
                    ),
                    block_id=block.block_id,
                    item_index=power_item_indexes[0],
                    headline=block.items[power_item_indexes[0]].headline,
                )
            )
        for idx, item in enumerate(block.items):
            detail_item_count += 1
            num_covered = len(item.covered_story_ids)
            covered_stories_detail += num_covered
            if num_covered > 1:
                multi_story_item_count += 1
            elif num_covered == 1:
                single_story_item_count += 1

            cited = [evidence[sid] for sid in item.cited_support_ids if sid in evidence]

            if len(item.body) > 650 and len(item.covered_fact_ids) >= 4:
                warnings.append(
                    DigestQualityWarning(
                        code="OVERLONG_SYNTHESIS",
                        message=(
                            "A dense multi-fact paragraph needs readable topic regrouping; "
                            "retain every supported fact and synthesize overlapping reports once."
                        ),
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

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

            item_full_text = f"{item.headline} {item.body}"
            if _SOURCE_META_NARRATION_RE.search(item_full_text):
                warnings.append(
                    DigestQualityWarning(
                        code="SOURCE_META_NARRATION",
                        message=(
                            "Describe the supported city development directly; do not narrate "
                            "separate source messages or list what posts mention."
                        ),
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

            if _TECHNICAL_TOKEN_RE.search(item_full_text):
                warnings.append(
                    DigestQualityWarning(
                        code="RAW_TECHNICAL_TOKEN",
                        message="Headline or body contains raw internal identifier/token.",
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

            if _TEMPORAL_CHAIN_RE.search(item_full_text):
                warnings.append(
                    DigestQualityWarning(
                        code="TEMPORAL_REPLAY_CHAIN",
                        message="Headline or body contains repetitive temporal replay chain (e.g. 'ранее также').",
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

            if len(_REPETITIVE_BODY_ATTRIBUTION_RE.findall(item.body)) > 1:
                warnings.append(
                    DigestQualityWarning(
                        code="REPETITIVE_BODY_ATTRIBUTION",
                        message="Body contains multiple repetitive attribution phrases.",
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

            if _CHAT_SLANG_OR_METADATA_RE.search(item_full_text):
                warnings.append(
                    DigestQualityWarning(
                        code="CHAT_SLANG_OR_METADATA",
                        message="Headline or body contains conversational chat slang or emoji chatter.",
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

            if _CLASSIFIED_AD_RE.search(item_full_text):
                warnings.append(
                    DigestQualityWarning(
                        code="CLASSIFIED_AD_LEAK",
                        message="Headline or body contains classified advertisement phrases.",
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

            if _PROFANITY_AND_ABUSE_RE.search(item_full_text):
                warnings.append(
                    DigestQualityWarning(
                        code="PROFANITY_OR_ABUSIVE_LANGUAGE",
                        message="Headline or body contains profanity, abusive language, or severe chat toxicity.",
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

            if _MALFORMED_CHAT_SYNTAX_RE.search(item_full_text):
                warnings.append(
                    DigestQualityWarning(
                        code="MALFORMED_CHAT_SYNTAX",
                        message="Headline or body contains malformed chat reply markers or broken grammar substitutions.",
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

            norm_hl = " ".join(item.headline.strip().casefold().split()).rstrip(" :.-")
            if _GENERIC_HEADLINE_RE.match(norm_hl):
                warnings.append(
                    DigestQualityWarning(
                        code="GENERIC_PLACEHOLDER_HEADLINE",
                        message=f"Headline '{item.headline}' is a generic placeholder and lacks specific topical value.",
                        block_id=block.block_id,
                        item_index=idx,
                        headline=item.headline,
                    )
                )

    # Check for repetitive generic headlines across blocks
    all_headlines = [
        item.headline.strip()
        for block in draft.blocks
        for item in block.items
        if item.headline and item.headline.strip()
    ]
    from collections import Counter

    hl_counts = Counter(" ".join(h.casefold().split()).rstrip(" :.-") for h in all_headlines)
    for norm_h, count in hl_counts.items():
        if count > 1:
            matching_hl = next(
                h for h in all_headlines if " ".join(h.casefold().split()).rstrip(" :.-") == norm_h
            )
            warnings.append(
                DigestQualityWarning(
                    code="REPETITIVE_GENERIC_HEADLINES",
                    message=f"Headline '{matching_hl}' is repeated {count} times across digest items.",
                    headline=matching_hl,
                )
            )

    # Check total text budget
    total_text_chars = sum(
        len(h) + len(b)
        for h, b in ((item.headline, item.body) for block in draft.blocks for item in block.items)
    )
    if total_text_chars > 3900:
        warnings.append(
            DigestQualityWarning(
                code="DIGEST_OVER_BUDGET",
                message=f"Total digest items character length ({total_text_chars}) exceeds Telegram single-post budget (max 3900 chars).",
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


def audit_rendered_digest(
    artifact: Any,
    draft: DigestNarrativeDraft,
    evidence: Mapping[str, PublicationEvidence],
    presentation_plan: Any,
    coverage_trace: Any,
    *,
    narrative_validation: Any | None = None,
) -> DigestQualityAudit:
    """Audit the whole canonical post; only completed mandatory checks can block.

    Semantic entailment and directory dominance are deliberately explicit
    NOT_EVALUATED outcomes until a reliable checker exists. They are not
    converted to PASS, and they do not suppress legitimate single-source PUBLISH
    community reports.
    """
    from src.publication.renderers import RenderedDigestArtifact

    if not isinstance(artifact, RenderedDigestArtifact):
        raise TypeError("audit_rendered_digest requires RenderedDigestArtifact")

    checks: list[DigestQualityCheck] = []

    def add(
        code: str,
        status: DigestCheckStatus,
        message: str,
        *,
        blocking: bool = False,
        text: str = "",
        evidence_ids: Sequence[str] = (),
    ) -> None:
        checks.append(
            DigestQualityCheck(
                code=code,
                status=status,
                blocking=blocking,
                message=message,
                text=text[:240],
                evidence_ids=tuple(dict.fromkeys(str(value) for value in evidence_ids)),
            )
        )

    prose = audit_digest_prose_quality(draft, evidence, presentation_plan)
    full_text = artifact.visible_text
    length_ok = artifact.visible_character_count <= 4096 and artifact.utf16_character_count <= 4096
    add(
        "TELEGRAM_SINGLE_POST_LIMIT",
        DigestCheckStatus.PASS if length_ok else DigestCheckStatus.FAIL,
        "Canonical visible post must fit the 4096-character Telegram ceiling.",
        blocking=True,
        text=(
            f"{artifact.visible_character_count} visible characters / "
            f"{artifact.utf16_character_count} UTF-16 units (limit 4096)"
        ),
    )

    entity_ranges = [(entity.offset, entity.offset + entity.length) for entity in artifact.entities]
    entity_valid = all(
        entity.offset >= 0
        and entity.length > 0
        and entity.offset + entity.length <= artifact.utf16_character_count
        for entity in artifact.entities
    ) and all(left[1] <= right[0] for left, right in zip(entity_ranges, entity_ranges[1:]))
    add(
        "TELEGRAM_ENTITY_OFFSETS",
        DigestCheckStatus.PASS if entity_valid else DigestCheckStatus.FAIL,
        "Formatting entity offsets are valid UTF-16 ranges in the canonical visible text.",
        blocking=True,
    )

    story_coverage = float(getattr(coverage_trace, "story_coverage", 0.0))
    add(
        "SELECTED_STORY_COVERAGE",
        DigestCheckStatus.PASS if story_coverage >= 1.0 else DigestCheckStatus.FAIL,
        "Every Story admitted to the frozen presentation plan must be represented.",
        blocking=True,
        text=f"{story_coverage:.3f}",
    )
    fact_coverage = float(getattr(coverage_trace, "material_fact_coverage", 0.0))
    add(
        "MATERIAL_FACT_COVERAGE",
        DigestCheckStatus.PASS if fact_coverage >= 1.0 else DigestCheckStatus.FAIL,
        "Every admitted required material fact must be represented.",
        blocking=True,
        text=f"{fact_coverage:.3f}",
        evidence_ids=tuple(
            support_id
            for fact in getattr(coverage_trace, "facts", ())
            if not getattr(fact, "covered", False)
            for support_id in getattr(fact, "required_support_ids", ())
        ),
    )

    community_publish_ids = {
        str(key)
        for key, value in evidence.items()
        if str(getattr(value, "kind", "")).lower() == "community_report"
        and str(getattr(value, "publication_use", "")).upper() == "PUBLISH"
    }
    fact_trace_by_id = {
        str(getattr(fact, "fact_id", "")): fact for fact in getattr(coverage_trace, "facts", ())
    }
    single_source_uncovered: list[str] = []
    for required_fact in getattr(presentation_plan, "required_facts", ()):
        fact_id = str(getattr(required_fact, "fact_id", ""))
        matched = [
            evidence[support_id]
            for support_id in getattr(required_fact, "support_ids", ())
            if support_id in community_publish_ids and support_id in evidence
        ]
        source_ids = {int(getattr(item, "source_id", 0)) for item in matched}
        if matched and len(source_ids) == 1:
            fact_trace = fact_trace_by_id.get(fact_id)
            if fact_trace is None or not bool(getattr(fact_trace, "covered", False)):
                single_source_uncovered.extend(
                    str(getattr(item, "evidence_id", "")) for item in matched
                )
    add(
        "SINGLE_SOURCE_PUBLISH_COMMUNITY_REPORTS",
        DigestCheckStatus.FAIL if single_source_uncovered else DigestCheckStatus.PASS,
        "Useful single-source PUBLISH community reports remain eligible; lack of corroboration or official confirmation is not an exclusion condition.",
        blocking=True,
        evidence_ids=single_source_uncovered,
    )

    if narrative_validation is not None:
        validation_ok = bool(getattr(narrative_validation, "is_valid", False))
        violations = tuple(getattr(narrative_validation, "violations", ()) or ())
        add(
            "EVIDENCE_AND_COMPOSITION_VALIDATION",
            DigestCheckStatus.PASS if validation_ok else DigestCheckStatus.FAIL,
            "Implemented evidence, composition, relation, and geography hard checks.",
            blocking=True,
            text="; ".join(str(value) for value in violations[:5]),
            evidence_ids=tuple(
                str(value).split(":", 1)[-1]
                for value in violations
                if "support" in str(value).lower() or "fact" in str(value).lower()
            ),
        )
        for diagnostic in getattr(narrative_validation, "not_evaluated", ()) or ():
            add(
                "SEMANTIC_BINDING_NOT_EVALUATED",
                DigestCheckStatus.NOT_EVALUATED,
                "The implementation cannot reliably resolve this semantic binding; it remains visible for review.",
                text=str(diagnostic),
            )
    else:
        add(
            "EVIDENCE_AND_COMPOSITION_VALIDATION",
            DigestCheckStatus.NOT_EVALUATED,
            "Narrative validation result was not provided to the rendered-post audit.",
        )

    item_by_location: dict[tuple[str | None, int | None], Any] = {}
    for block in draft.blocks:
        for index, item in enumerate(block.items):
            item_by_location[(block.block_id, index)] = item
    for warning in prose.warnings:
        warning_item = item_by_location.get((warning.block_id, warning.item_index))
        snippet = ""
        evidence_ids: Sequence[str] = ()
        if warning_item is not None:
            snippet = " ".join(part for part in (warning_item.headline, warning_item.body) if part)
            evidence_ids = tuple(getattr(warning_item, "cited_support_ids", ()))
        is_blocking = warning.code in _BLOCKING_PROSE_CODES
        add(
            warning.code,
            DigestCheckStatus.FAIL,
            warning.message,
            blocking=is_blocking,
            text=snippet or warning.headline or "",
            evidence_ids=evidence_ids,
        )

    duplicate_opening = re.search(
        r"(?m)^(⚡️|⚡|💧|📶|🚌|💥|📌)\s+\1(?:\s|$)", artifact.visible_text
    )
    add(
        "DUPLICATE_LEADING_EMOJI",
        DigestCheckStatus.FAIL if duplicate_opening else DigestCheckStatus.PASS,
        "An item should not repeat the same emoji label at its start.",
        text=duplicate_opening.group(0) if duplicate_opening else "",
    )

    caveat_patterns = (
        re.compile(r"\bна момент подготовки\b", re.IGNORECASE),
        re.compile(r"\bданные?\s+расходятся\b", re.IGNORECASE),
        re.compile(r"\bситуация\s+оста[её]тся\s+сложной\b", re.IGNORECASE),
    )
    repeated_caveat = next(
        (pattern.pattern for pattern in caveat_patterns if len(pattern.findall(full_text)) > 1),
        None,
    )
    add(
        "REPEATED_GENERIC_CAVEAT",
        DigestCheckStatus.FAIL if repeated_caveat else DigestCheckStatus.PASS,
        "Generic uncertainty caveats should not be repeated across the complete post.",
        text=repeated_caveat or "",
    )

    repeated_supports: dict[str, int] = {}
    for block in draft.blocks:
        for item in block.items:
            for support_id in set(getattr(item, "cited_support_ids", ())):
                repeated_supports[support_id] = repeated_supports.get(support_id, 0) + 1
    repeated = tuple(sorted(sid for sid, count in repeated_supports.items() if count > 1))
    add(
        "REPEATED_FACTS_SEMANTIC_REVIEW",
        DigestCheckStatus.NOT_EVALUATED,
        "Shared source references do not establish repeated reader-facing claims; semantic deduplication is not implemented.",
        evidence_ids=repeated,
    )

    directory_signals = sum(
        bool(pattern.search(full_text))
        for pattern in (
            re.compile(r"https?://|www\.", re.IGNORECASE),
            re.compile(r"\+?\d[\d\s()/-]{7,}\d"),
            re.compile(r"\b(?:цена|стоимость|руб(?:лей|\.)?|грн|₽|₴)\b", re.IGNORECASE),
            re.compile(r"\b(?:звоните|запись|заказ|куплю|продам|телефон)\b", re.IGNORECASE),
        )
    )
    add(
        "DIRECTORY_PAYLOAD_DOMINANCE",
        DigestCheckStatus.NOT_EVALUATED if directory_signals >= 2 else DigestCheckStatus.PASS,
        "Automatic checks cannot reliably distinguish a useful local service detail from directory payload.",
        text="directory-like signals detected" if directory_signals >= 2 else "",
    )

    add(
        "SEMANTIC_ENTAILMENT",
        DigestCheckStatus.NOT_EVALUATED,
        "No general semantic entailment checker is available; implemented exact/high-risk hard checks remain authoritative.",
    )

    return DigestQualityAudit(checks=tuple(checks), prose_audit=prose)
