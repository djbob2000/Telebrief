"""Free-form article writer backed by Story Cards and source excerpts."""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.ai_providers import AIProvider
from src.editorial_models import EditorialAnalysis, PreparedBundle, is_expected_language

if TYPE_CHECKING:
    from src.editorial_audit import FactCheckResult


@dataclass
class ArticleSection:
    heading: str
    paragraphs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AuditUnitLocator:
    unit_id: str
    path: tuple[str, ...]
    text: str


@dataclass
class ArticleDraft:
    headline: str
    lead: str
    paragraphs: list[str] = field(default_factory=list)
    sections: list[ArticleSection] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArticleDraft":  # noqa: C901
        if not isinstance(data, dict):
            raise ValueError("article draft must be an object")
        headline = data.get("headline")
        lead = data.get("lead")
        paragraphs = data.get("paragraphs", [])
        sections = data.get("sections", [])
        if isinstance(headline, dict):
            headline = headline.get("text")
        if isinstance(lead, dict):
            lead = lead.get("text")
        if isinstance(headline, str) and isinstance(lead, str):
            paragraphs = [
                item.get("text", "") if isinstance(item, dict) else item for item in paragraphs
            ]
            converted_sections = []
            for section in sections:
                if not isinstance(section, dict):
                    continue
                heading = section.get("heading", "")
                if isinstance(heading, dict):
                    heading = heading.get("text", "")
                converted_sections.append(
                    {
                        "heading": heading,
                        "paragraphs": [
                            item.get("text", "") if isinstance(item, dict) else item
                            for item in section.get("paragraphs", [])
                        ],
                    }
                )
            sections = converted_sections
        if not isinstance(headline, str) or not headline.strip().lstrip("#").strip():
            raise ValueError("article draft headline must be non-empty")
        if not isinstance(lead, str) or not lead.strip():
            raise ValueError("article draft lead must be non-empty")
        if not isinstance(paragraphs, list) or not all(
            isinstance(item, str) and item.strip() for item in paragraphs
        ):
            raise ValueError("article draft paragraphs must be non-empty strings")
        if not isinstance(sections, list):
            raise ValueError("article draft sections must be a list")
        parsed_sections: list[ArticleSection] = []
        for section in sections:
            if not isinstance(section, dict):
                raise ValueError("article section must be an object")
            heading = section.get("heading", "")
            section_paragraphs = section.get("paragraphs", [])
            if not isinstance(heading, str) or not heading.strip().lstrip("#").strip():
                raise ValueError("article section heading must be non-empty")
            if not isinstance(section_paragraphs, list) or not all(
                isinstance(item, str) and item.strip() for item in section_paragraphs
            ):
                raise ValueError("article section paragraphs must be non-empty strings")
            parsed_sections.append(
                ArticleSection(heading.strip().lstrip("#").strip(), section_paragraphs)
            )
        normalized_paragraphs = [] if parsed_sections else list(paragraphs)
        return cls(
            headline=headline.strip().lstrip("#").strip(),
            lead=lead.strip(),
            paragraphs=normalized_paragraphs,
            sections=parsed_sections,
        )

    @classmethod
    def from_json(cls, text: str) -> "ArticleDraft":
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
            cleaned = cleaned.removesuffix("```").strip()
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError("writer response is not valid JSON") from exc
        return cls.from_dict(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "lead": self.lead,
            "paragraphs": list(self.paragraphs),
            "sections": [
                {"heading": section.heading, "paragraphs": list(section.paragraphs)}
                for section in self.sections
            ],
        }

    def human_readable_text(self) -> str:
        parts = [self.headline, self.lead]
        parts.extend(self.paragraphs)
        for section in self.sections:
            parts.append(section.heading)
            parts.extend(section.paragraphs)
        return " ".join(p for p in parts if p)

    def to_markdown(self) -> str:
        clean_headline = self.headline.strip().lstrip("#").strip()
        blocks = [f"# {clean_headline}", self.lead]
        if self.sections:
            for section in self.sections:
                clean_heading = section.heading.strip().lstrip("#").strip()
                blocks.append(f"## {clean_heading}")
                blocks.extend(section.paragraphs)
        else:
            blocks.extend(self.paragraphs)
        return "\n\n".join(block.strip() for block in blocks if block.strip())

    def audit_units(self) -> dict[str, AuditUnitLocator]:
        units = {
            "TITLE": AuditUnitLocator("TITLE", ("headline",), self.headline),
            "LEAD": AuditUnitLocator("LEAD", ("lead",), self.lead),
        }
        paragraph_number = 1
        heading_number = 1
        if self.sections:
            for section_index, section in enumerate(self.sections):
                heading_id = f"H{heading_number:03d}"
                units[heading_id] = AuditUnitLocator(
                    heading_id, ("sections", str(section_index), "heading"), section.heading
                )
                heading_number += 1
                for paragraph_index, paragraph in enumerate(section.paragraphs):
                    unit_id = f"P{paragraph_number:03d}"
                    units[unit_id] = AuditUnitLocator(
                        unit_id,
                        ("sections", str(section_index), "paragraphs", str(paragraph_index)),
                        paragraph,
                    )
                    paragraph_number += 1
        else:
            for index, paragraph in enumerate(self.paragraphs):
                unit_id = f"P{paragraph_number:03d}"
                units[unit_id] = AuditUnitLocator(unit_id, ("paragraphs", str(index)), paragraph)
                paragraph_number += 1
        return units

    def apply_replacements(self, replacements: dict[str, str]) -> "ArticleDraft":
        result = copy.deepcopy(self)
        for unit_id, replacement in replacements.items():
            locator = result.audit_units().get(unit_id)
            if locator is None or not isinstance(replacement, str):
                continue
            value: Any = result
            for part in locator.path[:-1]:
                if part.isdigit():
                    value = value[int(part)]
                else:
                    value = getattr(value, part)
            last = locator.path[-1]
            if last.isdigit():
                value[int(last)] = replacement.strip()
            elif isinstance(value, list):
                value[int(last)] = replacement.strip()
            else:
                setattr(value, last, replacement.strip())
        return result


def render_story_contexts(story_contexts: dict[str, Any]) -> str:
    """Render deterministic local story context for the writer and fact checker."""
    if not story_contexts:
        return ""
    blocks: list[str] = []
    for card_id, ctx in sorted(story_contexts.items()):
        lines = [f"[LOCAL STORY CONTEXT {card_id}]"]
        if getattr(ctx, "municipal_areas", None):
            area_parts = [f"{a.area_id} ({len(a.source_refs)} refs)" for a in ctx.municipal_areas]
            lines.append(f"observed_municipal_areas: {', '.join(area_parts)}")
        if getattr(ctx, "colloquial_area_ids", None):
            lines.append(f"observed_colloquial_areas: {', '.join(ctx.colloquial_area_ids)}")
        scale = getattr(ctx, "scale", None)
        if scale:
            lines.append(
                f"scale_evidence: observed_count={scale.observed_count}, "
                f"geographic_spread={str(scale.geographic_spread).lower()}, "
                f"broad_prevalence_supported={str(scale.broad_prevalence_supported).lower()}, "
                f"majority_supported={str(scale.majority_supported).lower()}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


class EditorialWriter:
    """Write a natural article while keeping Story Cards as the evidence boundary."""

    def __init__(
        self,
        provider: AIProvider,
        model: str,
        skill_instructions: str,
        logger: logging.Logger,
        max_output_tokens: int = 65_536,
        output_language: str = "Russian",
    ):
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self.provider = provider
        self.model = model
        self.skill_instructions = skill_instructions
        self.logger = logger
        self.max_output_tokens = max_output_tokens
        self.output_language = output_language

    def build_prompt(
        self,
        analysis: EditorialAnalysis,
        bundle: PreparedBundle,
        revision_feedback: FactCheckResult | None = None,
    ) -> tuple[str, str]:
        system = f"""{self.skill_instructions}

Write the article exclusively in the configured output language: {self.output_language}.
All headlines, leads, section headings, and body paragraphs must be strictly written in {self.output_language}.

You are the article writer. Story Cards are reporting notes, not a sentence template.
Combine, reorder, compress and connect their material naturally into a cohesive 3–5 chapter article.
You may synthesize an editorial angle supported by several messages, but may not create a
new independently verifiable fact absent from the Story Cards and referenced source material.
Preserve attribution, uncertainty, contradiction, modality and source roles. Do not emit
internal identifiers, source refs, Markdown or commentary. Return strict JSON only with
headline, lead, paragraphs, and sections. When structuring into chapters/sections, leave
top-level paragraphs as empty [] to maintain a single canonical body. Use sections only
when real material supports them (usually 3–5 thematic chapters). Usually write 8–12 substantive paragraphs.
Usually prefer one or two materially informative unofficial versions. Include more only when
the additional versions materially change the reader’s understanding of the uncertainty.
Never enumerate speculation merely for completeness. Make the verified baseline explicit before
presenting unofficial estimates. Place significant unofficial versions in the relevant chapter
(use an optional end block 'Что пока не подтверждено' only when there are multiple significant
unresolved items worth summarizing).
Deterministic local story context ([LOCAL STORY CONTEXT]) specifies the observed geography and scale evidence for each Story Card. A street observation means the report came from that street/area, not that the entire area was affected. Same-area reports count as 1 area. Use majority or citywide phrasing only when majority_supported is true; when geographic_spread is true, describe as multiple areas (e.g. 'в нескольких районах города', 'в Центре и на Лисках'), not the whole city.
Scale language requires evidence of scale, not merely evidence of the underlying phenomenon.
Geographic spread ≠ broad prevalence ≠ majority: observations from several districts justify
'в нескольких районах' or 'в разных районах'; broad multi-district coverage allows 'во многих районах';
'в большинстве районов' requires explicit evidence establishing the majority denominator.
Absence from supplied reporting material does not prove absence in the outside world (corpus boundary).
When the evidence is corpus absence, write 'в доступных официальных сообщениях срок не указан',
'по доступным сообщениям редакции неизвестно', etc. Use absolute 'официального графика/срока нет' only
when an authorized source explicitly establishes that fact.
Normalize chat slang and colloquial contractions in author text (e.g. use 'дистанционное обучение / дистанционный формат'
instead of 'дистант', 'удалённая работа' instead of 'удалёнка', 'коммунальные службы' instead of 'коммуналка',
unless quoting verbatim resident speech in quotes).
Aim for about 900–1500 words on a busy day, allow up to about 1800 words when the
material genuinely supports it, and accept 600–900 words on a thin day. These are editorial
targets, not validation limits; never pad length.
"""
        user_parts = [
            "STORY CARDS:\n" + json.dumps(analysis.to_dict(), ensure_ascii=False),
        ]
        story_ctx_str = render_story_contexts(getattr(bundle, "story_contexts", {}))
        if story_ctx_str:
            user_parts.append("LOCAL STORY CONTEXT:\n" + story_ctx_str)
        user_parts.append("ORIGINAL SOURCE EXCERPTS:\n" + bundle.prompt_text)
        if revision_feedback and revision_feedback.issues:
            fix_issues = [issue for issue in revision_feedback.issues if issue.severity == "fix"]
            if fix_issues:
                feedback_lines = ["AUDIT REVISION FEEDBACK:"]
                for issue in fix_issues:
                    feedback_lines.append(f"- {issue.unit_id} / {issue.code}")
                    if issue.reason:
                        feedback_lines.append(f"  Reason: {issue.reason}")
                    if issue.suggested_direction:
                        feedback_lines.append(f"  Direction: {issue.suggested_direction}")
                feedback_lines.extend(
                    [
                        "",
                        "These are failure modes to correct, not replacement sentences.",
                        "Do not mechanically copy suggested wording.",
                        "Do not introduce facts absent from Story Cards and source records.",
                    ]
                )
                user_parts.append("\n".join(feedback_lines))
        user = "\n\n".join(user_parts)
        return system, user

    async def write(
        self,
        analysis: EditorialAnalysis,
        bundle: PreparedBundle,
        revision_feedback: FactCheckResult | None = None,
    ) -> ArticleDraft:
        system, user = self.build_prompt(analysis, bundle, revision_feedback=revision_feedback)
        response = await self.provider.chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=self.model,
            temperature=0.7,
            max_tokens=self.max_output_tokens,
            response_format={"type": "json_object"},
        )
        draft = ArticleDraft.from_json(response)
        if any(
            not is_expected_language(unit.text, self.output_language)
            for unit in draft.audit_units().values()
        ):
            raise ValueError(f"writer output language mismatch: expected {self.output_language}")
        return draft
