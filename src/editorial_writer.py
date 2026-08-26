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
            if (
                not isinstance(section_paragraphs, list)
                or not section_paragraphs
                or not all(isinstance(item, str) and item.strip() for item in section_paragraphs)
            ):
                raise ValueError(
                    "article section paragraphs must contain at least one non-empty string"
                )
            parsed_sections.append(
                ArticleSection(
                    heading.strip().lstrip("#").strip(),
                    [
                        item.strip()
                        for item in section_paragraphs
                        if isinstance(item, str) and item.strip()
                    ],
                )
            )
        normalized_paragraphs = (
            []
            if parsed_sections
            else [item.strip() for item in paragraphs if isinstance(item, str) and item.strip()]
        )
        if not parsed_sections and not normalized_paragraphs:
            raise ValueError("article draft must contain at least one body paragraph")
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

    def to_markdown(self, include_title: bool = True) -> str:
        clean_headline = self.headline.strip().lstrip("#").strip()
        blocks: list[str] = [f"# {clean_headline}", self.lead] if include_title else [self.lead]
        if self.sections:
            for section in self.sections:
                clean_heading = section.heading.strip().lstrip("#").strip()
                blocks.append(f"## {clean_heading}")
                blocks.extend(section.paragraphs)
        else:
            blocks.extend(self.paragraphs)
        return "\n\n".join(block.strip() for block in blocks if block.strip())

    def to_body_markdown(self) -> str:
        """Return the article body (lead and sections) without the top-level headline."""
        return self.to_markdown(include_title=False)

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
        reasoning_effort: str | None = None,
        temperature: float | None = None,
    ):
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self.provider = provider
        self.model = model
        self.skill_instructions = skill_instructions
        self.logger = logger
        self.max_output_tokens = max_output_tokens
        self.output_language = output_language
        self.reasoning_effort = reasoning_effort
        self.temperature = temperature

    def build_prompt(
        self,
        analysis: EditorialAnalysis,
        bundle: PreparedBundle,
        revision_feedback: FactCheckResult | None = None,
        historical_background: str = "",
    ) -> tuple[str, str]:
        system = f"""{self.skill_instructions}

Write the article exclusively in the configured output language: {self.output_language}.
All headlines, leads, section headings, and body paragraphs must be strictly written in {self.output_language}.

You are the article writer. Story Cards are reporting notes, not a sentence template.
Combine, reorder, compress and connect their material naturally into a cohesive 3–5 chapter article.
You may synthesize an editorial angle supported by several messages, but may not create a
new independently verifiable fact absent from the Story Cards and referenced source material.
Preserve attribution, uncertainty, contradiction, modality and source roles. Do not emit
internal identifiers, source refs, Markdown or commentary.

Return strict JSON only matching this canonical shape:
{{
  "headline": "...",
  "lead": "...",
  "paragraphs": [],
  "sections": [
    {{
      "heading": "...",
      "paragraphs": [
        "First substantive body paragraph for this section...",
        "Second substantive body paragraph for this section..."
      ]
    }}
  ]
}}

CRITICAL STRUCTURAL RULES:
1. Every section in "sections" MUST contain non-empty body paragraphs in its "paragraphs" array. Never return empty "paragraphs": [].
2. When structuring into chapters/sections (usually 3–5 thematic chapters), place all substantive body paragraphs inside their respective section "paragraphs" arrays, and keep top-level "paragraphs" as [].
3. If writing a short single-topic brief without chapters, place all body paragraphs in top-level "paragraphs" and leave "sections" as [].
4. Usually write 8–12 substantive paragraphs across the entire article.
5. Prioritization and Headline Promise: The headline and Chapter 1 MUST lead with the biggest citywide development affecting daily life (e.g. multi-week blackout, utilities, safety crisis). Secondary infrastructure works or localized road detours come in subsequent chapters.
6. Actionable Resident Contacts: ALWAYS preserve practical contact and service details present in evidence: phone numbers (e.g. +7 (990)...), precise building addresses, operating hours, and required documents.
7. Dual Street Naming: For renamed or historically dual-named streets in Berdyansk, use natural dual naming (e.g. «улица Тверская (бывшая Карла Маркса)»).

Source attribution and collection mechanics:
Do NOT reveal internal collection mechanics to the reader. Avoid phrases like 'в чате', 'участники чата',
'сообщение в чате', 'в Telegram-чате обсуждают', 'один из участников написал'. Use natural journalistic
city attribution: 'жители сообщают', 'горожане обсуждают', 'по словам жителей', 'среди родителей обсуждается',
'по наблюдениям горожан'. Core principle: Do not hide uncertainty — hide the technical method of data collection.

Official forwarded messages:
If a source record contains forward_origin with the name or username of an official organization or utility
(e.g. 'Запорожгаз', 'МЧС', 'Администрация'), attribute the information directly to that official source
(e.g. '«Запорожгаз» предупредил...', 'Бердянский участок «Запорожгаза» сообщил...'). Do NOT artificially
downgrade it to 'опубликовали сообщение от имени организации' merely because it was received as a forward.

Source-person attribute fidelity:
Preserve explicit person attributes from the evidence, including gendered attribution.
Do not change "житель" to "жительница", "мужчина" to "женщина", or the reverse for stylistic variety.

Do not infer gender from a Telegram/display name, username, avatar, profile style, or narrative convenience.
Use a gendered noun only when the source wording itself or explicit trusted metadata establishes it.
If gender is not established, rewrite impersonally rather than inventing one.

Example: if evidence only establishes that water was reported from the military town, prefer an
impersonal construction such as "В военном городке воду отключили вместе со светом в ночь на 1 августа"
over inventing "жительница военного городка рассказала...".

Weaving uncertainty into narrative chapters:
Do NOT create a separate checklist section like 'Что пока не подтверждено' or 'Что не подтверждено'.
Weave rumors, unofficial versions, and uncertainties directly and naturally into their relevant thematic story
chapters (e.g. rumors about transformer relocation inside the power outage chapter, versions about school formats
inside the education chapter). Usually prefer one or two materially informative unofficial versions. Include more
only when the additional versions materially change the reader's understanding of the uncertainty. Never enumerate
speculation merely for completeness. Make the verified baseline explicit before presenting unofficial estimates.

Direct quotes and human voice:
Actively use verbatim direct quotes (typically 2–4 sharp, authentic quotes across the article) from residents in ORIGINAL SOURCE EXCERPTS. Quotes are essential to convey the living human reality, genuine emotions, neighborly debates, and daily struggles (e.g. storing insulin, caring for elderly relatives, debates over generator noise and exhaust in residential courtyards, or anxiety about winter). Paraphrase the rest in clear journalistic language.

Community mood, resident concerns, and everyday coping:
When covering municipal crises (such as multi-week blackouts, water shortages, connectivity outages, or price surges), fully integrate residents' genuine concerns, seasonal anxieties (such as winter coping, stove heating, preparing supplies, or rural alternatives), everyday adaptations, and neighborhood debates into the narrative chapters. Highlight tangible micro-details present in source records: working ATMs with backup power, emergency generator hours, water pressure contrasts between upper floors and lowlands, and practical adaptations. Distinguish genuine community sentiment from statistical claims: describe the qualitative concern accurately without turning emotional hyperbole or conversational figures of speech (e.g. '80% сбегут') into pseudo-statistics or demographic facts.

Resident experience and technical advice:
Present technical lifehacks, practical tips, and community explanations (such as powering routers from powerbanks,
voltage specs, 'flight mode' toggling for cell towers, or optic fiber vs twisted pair) as resident experience, advice, or observations ('По опыту жителей...',
'Как отмечают горожане...'), not as universal technical laws.

Deterministic local story context ([LOCAL STORY CONTEXT]) specifies the observed geography and scale evidence for each Story Card. A street observation means the report came from that street/area, not that the entire area was affected. Same-area reports count as 1 area. Use majority or citywide phrasing only when majority_supported is true; when geographic_spread is true, describe as multiple areas (e.g. 'в нескольких районах города', 'в Центре и на Лисках'), not the whole city.
Scale language requires evidence of scale, not merely evidence of the underlying phenomenon.
Geographic spread ≠ broad prevalence ≠ majority: observations from several districts justify
'в нескольких районах' or 'в разных районах'; broad multi-district coverage allows 'во многих районах';
'в большинстве районов' requires explicit evidence establishing the majority denominator.
Absence from supplied reporting material does not prove absence in the outside world (corpus boundary).
Corpus-boundary uncertainty must be expressed as natural publication copy without revealing collection mechanics. Prefer “точные сроки пока неизвестны”, “официальные сроки пока не назывались”, “подтверждённой информации пока нет”, or an equally cautious formulation supported by the evidence. Never write “в доступных сообщениях”, “в предоставленных материалах”, “в собранных сообщениях”, “в исходных записях”, “в нашем корпусе”, or similar internal-source language. Use absolute 'официального графика/срока нет' only when an authorized source explicitly establishes that fact.

When source material contains materially different concrete values for the same metric (such as prices or times), prefer one concise contrast using one or two actual representative values, e.g. “одни называли X, другие Y” or “назывались X и Y”. Never average, interpolate, invent a midpoint or range endpoint, or fabricate a second value. Use only concrete values actually present in the source records. If the actual values are not preserved in Story Cards/source excerpts, keep the disagreement qualitative.
Normalize chat slang and colloquial contractions in author text (e.g. use 'дистанционное обучение / дистанционный формат'
instead of 'дистант', 'удалённая работа' instead of 'удалёнка', 'коммунальные службы' instead of 'коммуналка',
unless quoting verbatim resident speech in quotes).

Local topography and named places:
Do not invent a neighborhood or district name by mechanically joining a named street/prospect
with a colloquial topographic descriptor such as "низ", "верх", "внизу", "гора", or
"нижняя часть города". Treat the named place and the topographic position as separate facts.

For Berdyansk, if source wording is shorthand such as "Пролетарский низ" and local context
identifies "Пролетарский" as a prospect, do not write author copy such as
"жительница Пролетарского низа". Prefer a natural form such as
"жительница с проспекта Пролетарского в нижней части города" or, when the topographic detail
is not important, simply "жительница с проспекта Пролетарского".

This normalization applies to author prose only. Preserve an exact colloquial phrase when it is
intentionally used inside a faithful direct quote.

Local proper-name fidelity:
When source material gives a named local place, market, park, stop, provider, street,
landmark, or colloquial place label, preserve its lexical identity in author prose.
Do not reinterpret a proper name into a semantically related common noun or a more
"literary" description.

Example: source wording for "Железный рынок" (such as "у Железного рынка") may be grammatically inflected as needed,
but must not become "у железнодорожного рынка" or "железнодорожный рынок" unless the source/local context actually
names a different place that way.

Grammatical case, prepositions, and capitalization may change when Russian grammar requires it;
the named-place identity and meaning must not change. If local context provides a canonical
alias, use that known alias rather than inventing a new descriptive name.

Historical background context (HISTORICAL BACKGROUND):
When historical background from past years is provided in the prompt, use it strictly as historical background, context, or prehistory (e.g. 'Напомним, в 2023 году...', 'Проблемы с этой подстанцией фиксировались еще в прошлом году...'). Never confuse past archive events with today's breaking news or present them as today's events.

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
        if historical_background and historical_background.strip():
            user_parts.append(
                "HISTORICAL BACKGROUND (ARCHIVE / СПРАВКА И БЭКГРАУНД):\n"
                + historical_background.strip()
            )
        user_parts.append("ORIGINAL SOURCE EXCERPTS:\n" + bundle.prompt_text)
        if revision_feedback and revision_feedback.issues:
            fix_issues = [issue for issue in revision_feedback.issues if issue.severity == "fix"]
            if fix_issues:
                feedback_lines = ["AUDIT REVISION FEEDBACK:"]
                for issue in fix_issues:
                    feedback_lines.append(f"- {issue.unit_id} / {issue.code}")
                    blocking_str = (
                        "true" if getattr(issue, "publication_blocking", False) else "false"
                    )
                    feedback_lines.append(f"  Publication blocking: {blocking_str}")
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
        historical_background: str = "",
    ) -> ArticleDraft:
        system, user = self.build_prompt(
            analysis,
            bundle,
            revision_feedback=revision_feedback,
            historical_background=historical_background,
        )
        response = await self.provider.chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_output_tokens,
            reasoning_effort=self.reasoning_effort,
            response_format={"type": "json_object"},
        )
        draft = ArticleDraft.from_json(response)
        if any(
            not is_expected_language(unit.text, self.output_language)
            for unit in draft.audit_units().values()
        ):
            raise ValueError(f"writer output language mismatch: expected {self.output_language}")
        return draft
