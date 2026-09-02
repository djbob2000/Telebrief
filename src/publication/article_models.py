"""Domain and output models for structured editorial articles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

_INTERNAL_EVIDENCE_ID_RE = re.compile(
    r"\[(?:story:\d+:evidence:\d+:frag:\d+|story:\d+|evidence:\d+:frag:\d+|op:[^\]]+)\]",
    re.IGNORECASE,
)


ArticleGenerationOrigin = Literal["AI", "SUPPLEMENT", "FALLBACK"]


@dataclass(frozen=True)
class ArticleClaimAtom:
    """An atomic factual claim with explicit supporting evidence/support IDs."""

    text: str
    cited_support_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ArticleClaimAtom | None:
        if not isinstance(data, dict):
            return None
        text = _strip_internal_handles(str(data.get("text", "")).strip())
        raw_ids = data.get("cited_support_ids") or data.get("cited_evidence_ids") or []
        if isinstance(raw_ids, (str, int)):
            raw_ids = [raw_ids]
        support_ids = tuple(
            dict.fromkeys(
                str(cid).strip()
                for cid in raw_ids
                if cid and isinstance(cid, (str, int)) and str(cid).strip()
            )
        )
        if not text or not support_ids:
            return None
        return cls(text=text, cited_support_ids=support_ids)


def _parse_claim_atoms(raw: Any) -> tuple[ArticleClaimAtom, ...]:
    if not isinstance(raw, list):
        return ()
    atoms: list[ArticleClaimAtom] = []
    for item in raw:
        atom = ArticleClaimAtom.from_dict(item)
        if atom is not None:
            atoms.append(atom)
    return tuple(atoms)


@dataclass(frozen=True)
class ArticleParagraph:
    """A single factual paragraph with explicit cited support IDs."""

    text: str
    cited_support_ids: tuple[str, ...] = ()
    claims: tuple[ArticleClaimAtom, ...] = ()
    generation_origin: ArticleGenerationOrigin = "AI"


@dataclass(frozen=True)
class ArticleSection:
    """A discrete thematic section within an editorial article."""

    heading: str
    heading_support_ids: tuple[str, ...] = ()
    heading_claims: tuple[ArticleClaimAtom, ...] = ()
    paragraphs: tuple[ArticleParagraph, ...] = ()
    cited_evidence_ids: tuple[str, ...] = ()
    heading_generation_origin: ArticleGenerationOrigin = "AI"


@dataclass(frozen=True)
class StructuredArticleDraft:
    """Structured editorial article draft with unit-level support provenance."""

    title: str
    title_support_ids: tuple[str, ...]
    lead: str
    lead_support_ids: tuple[str, ...]
    sections: tuple[ArticleSection, ...]
    title_claims: tuple[ArticleClaimAtom, ...] = ()
    lead_claims: tuple[ArticleClaimAtom, ...] = ()
    cited_evidence_ids: tuple[str, ...] = ()
    word_count: int = 0
    title_generation_origin: ArticleGenerationOrigin = "AI"
    lead_generation_origin: ArticleGenerationOrigin = "AI"

    def __post_init__(self) -> None:
        if self.word_count == 0:
            all_text = " ".join(
                [self.title, self.lead] + [p.text for s in self.sections for p in s.paragraphs]
            )
            object.__setattr__(self, "word_count", len(all_text.split()))

    @property
    def cited_support_ids(self) -> tuple[str, ...]:
        """All unique support IDs cited across title, lead, headings, and paragraphs."""
        ids: list[str] = []
        ids.extend(self.title_support_ids)
        ids.extend(self.lead_support_ids)
        for sec in self.sections:
            ids.extend(sec.heading_support_ids)
            for p in sec.paragraphs:
                ids.extend(p.cited_support_ids)
        return tuple(dict.fromkeys(ids))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StructuredArticleDraft:
        """Parse structured article draft from model JSON dictionary."""
        title = _strip_internal_handles(str(data.get("title", "")).strip())
        raw_t_ids = data.get("title_support_ids") or data.get("title_evidence_ids") or []
        title_support_ids = tuple(
            dict.fromkeys(
                str(cid).strip()
                for cid in raw_t_ids
                if cid and isinstance(cid, (str, int)) and str(cid).strip()
            )
        )
        title_claims = _parse_claim_atoms(data.get("title_claims"))
        if not title_claims and title and title_support_ids:
            title_claims = (ArticleClaimAtom(text=title, cited_support_ids=title_support_ids),)

        lead = _strip_internal_handles(str(data.get("lead", "")).strip())
        raw_l_ids = data.get("lead_support_ids") or data.get("lead_evidence_ids") or []
        lead_support_ids = tuple(
            dict.fromkeys(
                str(cid).strip()
                for cid in raw_l_ids
                if cid and isinstance(cid, (str, int)) and str(cid).strip()
            )
        )
        lead_claims = _parse_claim_atoms(data.get("lead_claims"))
        if not lead_claims and lead and lead_support_ids:
            lead_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", lead) if s.strip()]
            lead_claims = tuple(
                ArticleClaimAtom(text=s, cited_support_ids=lead_support_ids)
                for s in (lead_sentences or [lead])
            )

        sections_list: list[ArticleSection] = []
        all_legacy_cited: list[str] = []

        raw_sections = data.get("sections", [])
        if isinstance(raw_sections, list):
            for sec_data in raw_sections:
                if not isinstance(sec_data, dict):
                    continue
                heading = _strip_internal_handles(str(sec_data.get("heading", "")).strip())
                raw_h_ids = (
                    sec_data.get("heading_support_ids")
                    or sec_data.get("heading_evidence_ids")
                    or []
                )
                heading_support_ids = tuple(
                    dict.fromkeys(
                        str(cid).strip()
                        for cid in raw_h_ids
                        if cid and isinstance(cid, (str, int)) and str(cid).strip()
                    )
                )
                heading_claims = _parse_claim_atoms(sec_data.get("heading_claims"))
                if not heading_claims and heading and heading_support_ids:
                    heading_claims = (
                        ArticleClaimAtom(text=heading, cited_support_ids=heading_support_ids),
                    )

                raw_paras = sec_data.get("paragraphs", [])
                paras: list[ArticleParagraph] = []
                if isinstance(raw_paras, list):
                    for p in raw_paras:
                        if isinstance(p, dict):
                            p_text = _strip_internal_handles(str(p.get("text", "")).strip())
                            raw_p_ids = (
                                p.get("cited_support_ids") or p.get("cited_evidence_ids") or []
                            )
                            p_support_ids = tuple(
                                dict.fromkeys(
                                    str(cid).strip()
                                    for cid in raw_p_ids
                                    if cid and isinstance(cid, (str, int)) and str(cid).strip()
                                )
                            )
                            p_claims = _parse_claim_atoms(p.get("claims"))
                            if not p_claims and p_text and p_support_ids:
                                p_sentences = [
                                    s.strip()
                                    for s in re.split(r"(?<=[.!?])\s+", p_text)
                                    if s.strip()
                                ]
                                p_claims = tuple(
                                    ArticleClaimAtom(text=s, cited_support_ids=p_support_ids)
                                    for s in (p_sentences or [p_text])
                                )

                            if p_text:
                                paras.append(
                                    ArticleParagraph(
                                        text=p_text,
                                        cited_support_ids=p_support_ids,
                                        claims=p_claims,
                                    )
                                )
                        elif isinstance(p, str):
                            p_str = _strip_internal_handles(p.strip())
                            if p_str:
                                paras.append(
                                    ArticleParagraph(
                                        text=p_str,
                                        cited_support_ids=(),
                                        claims=(),
                                    )
                                )

                sec_legacy_cited = [
                    str(cid).strip()
                    for cid in sec_data.get("cited_evidence_ids", [])
                    if cid and isinstance(cid, (str, int))
                ]
                all_legacy_cited.extend(sec_legacy_cited)
                if heading or paras:
                    sections_list.append(
                        ArticleSection(
                            heading=heading,
                            heading_support_ids=heading_support_ids,
                            heading_claims=heading_claims,
                            paragraphs=tuple(paras),
                            cited_evidence_ids=tuple(sec_legacy_cited),
                        )
                    )

        top_level_cited = [
            str(cid).strip()
            for cid in data.get("cited_evidence_ids", [])
            if cid and isinstance(cid, (str, int))
        ]
        combined_legacy_cited = tuple(dict.fromkeys(top_level_cited + all_legacy_cited))

        all_text = " ".join([title, lead] + [p.text for s in sections_list for p in s.paragraphs])
        word_count = len(all_text.split())

        return cls(
            title=title,
            title_support_ids=title_support_ids,
            title_claims=title_claims,
            lead=lead,
            lead_support_ids=lead_support_ids,
            lead_claims=lead_claims,
            sections=tuple(sections_list),
            cited_evidence_ids=combined_legacy_cited,
            word_count=word_count,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert structured article draft to JSON dictionary."""
        return {
            "title": self.title,
            "title_support_ids": list(self.title_support_ids),
            "lead": self.lead,
            "lead_support_ids": list(self.lead_support_ids),
            "sections": [
                {
                    "heading": s.heading,
                    "heading_support_ids": list(s.heading_support_ids),
                    "paragraphs": [
                        {
                            "text": p.text,
                            "cited_support_ids": list(p.cited_support_ids),
                        }
                        for p in s.paragraphs
                    ],
                }
                for s in self.sections
            ],
            "word_count": self.word_count,
            "title_generation_origin": self.title_generation_origin,
            "lead_generation_origin": self.lead_generation_origin,
        }

    def render_markdown(self) -> str:
        """Render clean user-facing markdown WITHOUT internal evidence IDs."""
        lines: list[str] = []
        if self.lead:
            clean_lead = _strip_internal_handles(self.lead)
            if clean_lead:
                lines.append(clean_lead)
                lines.append("")

        for section in self.sections:
            if section.heading:
                clean_h = _strip_internal_handles(section.heading)
                if clean_h:
                    lines.append(f"## {clean_h}")
                    lines.append("")
            for p in section.paragraphs:
                clean_p = _strip_internal_handles(p.text)
                if clean_p:
                    lines.append(clean_p)
                    lines.append("")

        return "\n".join(lines).strip()


def _strip_internal_handles(text: str) -> str:
    """Strip any lingering internal [story:...] or [op:...] evidence handles from user-visible text."""
    if not text:
        return ""
    cleaned = _INTERNAL_EVIDENCE_ID_RE.sub("", text)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    cleaned = re.sub(r" \.", ".", cleaned)
    cleaned = re.sub(r" ,", ",", cleaned)
    return cleaned.strip()
