"""Free-form article writer backed by Story Cards and source excerpts."""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from src.ai_providers import AIProvider
from src.editorial_models import EditorialAnalysis, PreparedBundle


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
        if not isinstance(headline, str) or not headline.strip():
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
            if not isinstance(heading, str) or not heading.strip():
                raise ValueError("article section heading must be non-empty")
            if not isinstance(section_paragraphs, list) or not all(
                isinstance(item, str) and item.strip() for item in section_paragraphs
            ):
                raise ValueError("article section paragraphs must be non-empty strings")
            parsed_sections.append(ArticleSection(heading.strip(), section_paragraphs))
        normalized_paragraphs = [] if parsed_sections else list(paragraphs)
        return cls(headline.strip(), lead.strip(), normalized_paragraphs, parsed_sections)

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

    def to_markdown(self) -> str:
        blocks = [f"# {self.headline}", self.lead]
        if self.sections:
            for section in self.sections:
                blocks.append(f"## {section.heading}")
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


class EditorialWriter:
    """Write a natural article while keeping Story Cards as the evidence boundary."""

    def __init__(
        self,
        provider: AIProvider,
        model: str,
        skill_instructions: str,
        logger: logging.Logger,
        max_output_tokens: int = 65_536,
    ):
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self.provider = provider
        self.model = model
        self.skill_instructions = skill_instructions
        self.logger = logger
        self.max_output_tokens = max_output_tokens

    def build_prompt(self, analysis: EditorialAnalysis, bundle: PreparedBundle) -> tuple[str, str]:
        system = f"""{self.skill_instructions}

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
Scale language requires evidence of scale, not merely evidence of the underlying phenomenon.
Support may come from quantified official/news reporting or sufficiently broad independent
observations across locations/source roles. A few comments, advertisements, or repeated reposts
cannot establish citywide scale, shortage, demand growth, or majority behavior.
Aim for about 900–1500 words on a busy day, allow up to about 1800 words when the
material genuinely supports it, and accept 600–900 words on a thin day. These are editorial
targets, not validation limits; never pad length.
"""
        user = (
            "STORY CARDS:\n"
            + json.dumps(analysis.to_dict(), ensure_ascii=False)
            + "\n\nORIGINAL SOURCE EXCERPTS:\n"
            + bundle.prompt_text
        )
        return system, user

    async def write(self, analysis: EditorialAnalysis, bundle: PreparedBundle) -> ArticleDraft:
        system, user = self.build_prompt(analysis, bundle)
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
        return ArticleDraft.from_json(response)
