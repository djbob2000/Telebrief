"""Domain and output models for structured editorial articles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

_INTERNAL_EVIDENCE_ID_RE = re.compile(
    r"\[(?:story:\d+:evidence:\d+:frag:\d+|story:\d+|evidence:\d+:frag:\d+)\]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ArticleSection:
    """A discrete thematic section within an editorial article."""

    heading: str
    paragraphs: tuple[str, ...]
    cited_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StructuredArticleDraft:
    """Structured editorial article draft with exact evidence provenance."""

    title: str
    lead: str
    sections: tuple[ArticleSection, ...]
    cited_evidence_ids: tuple[str, ...] = ()
    word_count: int = 0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StructuredArticleDraft:
        """Parse structured article draft from model JSON dictionary."""
        title = _strip_internal_handles(str(data.get("title", "")).strip())
        lead = _strip_internal_handles(str(data.get("lead", "")).strip())

        sections_list: list[ArticleSection] = []
        all_cited: list[str] = []

        raw_sections = data.get("sections", [])
        if isinstance(raw_sections, list):
            for sec_data in raw_sections:
                if not isinstance(sec_data, dict):
                    continue
                heading = _strip_internal_handles(str(sec_data.get("heading", "")).strip())
                raw_paras = sec_data.get("paragraphs", [])
                paras: list[str] = []
                if isinstance(raw_paras, list):
                    for p in raw_paras:
                        p_str = _strip_internal_handles(str(p).strip())
                        if p_str:
                            paras.append(p_str)

                cited = [
                    str(cid).strip()
                    for cid in sec_data.get("cited_evidence_ids", [])
                    if cid and isinstance(cid, (str, int))
                ]
                all_cited.extend(cited)
                if heading or paras:
                    sections_list.append(
                        ArticleSection(
                            heading=heading,
                            paragraphs=tuple(paras),
                            cited_evidence_ids=tuple(cited),
                        )
                    )

        top_level_cited = [
            str(cid).strip()
            for cid in data.get("cited_evidence_ids", [])
            if cid and isinstance(cid, (str, int))
        ]
        combined_cited = tuple(dict.fromkeys(top_level_cited + all_cited))

        all_text = " ".join([title, lead] + [p for s in sections_list for p in s.paragraphs])
        word_count = len(all_text.split())

        return cls(
            title=title,
            lead=lead,
            sections=tuple(sections_list),
            cited_evidence_ids=combined_cited,
            word_count=word_count,
        )

    def render_markdown(self) -> str:
        """Render clean user-facing markdown WITHOUT internal evidence IDs."""
        lines: list[str] = []
        if self.lead:
            lines.append(self.lead)
            lines.append("")

        for section in self.sections:
            if section.heading:
                lines.append(f"## {section.heading}")
                lines.append("")
            for p in section.paragraphs:
                clean_p = _strip_internal_handles(p)
                if clean_p:
                    lines.append(clean_p)
                    lines.append("")

        return "\n".join(lines).strip()


def _strip_internal_handles(text: str) -> str:
    """Strip any lingering internal [story:...] evidence handles from user-visible text."""
    if not text:
        return ""
    cleaned = _INTERNAL_EVIDENCE_ID_RE.sub("", text)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    cleaned = re.sub(r" \.", ".", cleaned)
    cleaned = re.sub(r" ,", ",", cleaned)
    return cleaned.strip()
