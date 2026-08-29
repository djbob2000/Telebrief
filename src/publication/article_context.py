"""Editorial context packaging for long-form article synthesis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.domain.operational_state import ResolvedObservation
from src.editorial_models import StoryCard
from src.publication.digest_contracts import GENERIC_FALLBACK_TOPICS
from src.publication.evidence import PublicationEvidence


@dataclass(frozen=True)
class ArticleEditorialContext:
    """Rich, structured editorial context for long-form article generation."""

    headline_candidates: tuple[str, ...]
    operational_timeline: tuple[ResolvedObservation, ...]
    evidence_index: tuple[PublicationEvidence, ...]
    evidence_by_id: dict[str, PublicationEvidence]
    recurring_topics: tuple[str, ...]
    general_facts: tuple[PublicationEvidence, ...]
    resident_observations: tuple[PublicationEvidence, ...]

    def to_prompt_context(self) -> str:
        """Render deterministic, structured context for the single article writing LLM call."""
        sections: list[str] = []

        if self.operational_timeline:
            sections.append("## Городская оперативная хроника (коммунальная ситуация)")
            for obs in self.operational_timeline:
                ts_str = obs.observed_at.strftime("%H:%M")
                loc_str = f" [{obs.observation.location}]" if obs.observation.location else ""
                state_str = f" ({obs.observation.state})" if obs.observation.state else ""
                sections.append(
                    f"- {ts_str}{loc_str}{state_str}: {obs.observation.detail} "
                    f"(refs: {', '.join(obs.source_refs)})"
                )
            sections.append("")

        if self.general_facts:
            sections.append("## Установленные факты и события")
            for evi in self.general_facts:
                sections.append(f"- [{evi.evidence_id}] {evi.text} (ref: {evi.source_ref})")
            sections.append("")

        if self.resident_observations:
            sections.append("## Наблюдения и сообщения жителей")
            for evi in self.resident_observations:
                sections.append(f"- [{evi.evidence_id}] {evi.text} (ref: {evi.source_ref})")
            sections.append("")

        return "\n".join(sections).strip()


def build_article_editorial_context(
    cards: Sequence[StoryCard],
    evidence_items: Sequence[PublicationEvidence],
    operational_observations: Sequence[ResolvedObservation] = (),
) -> ArticleEditorialContext:
    """Build structured ArticleEditorialContext from frozen cards and publication evidence."""
    evidence_by_id: dict[str, PublicationEvidence] = {
        evi.evidence_id: evi for evi in evidence_items
    }

    # Filter out generic fallback titles from headline candidates
    headlines: list[str] = []
    seen_headlines: set[str] = set()
    for card in cards:
        cand = (card.topic or card.summary or "").strip()
        if cand and cand.lower() not in {t.lower() for t in GENERIC_FALLBACK_TOPICS}:
            if cand not in seen_headlines:
                seen_headlines.add(cand)
                headlines.append(cand)

    # Separate evidence items by kind
    general_facts: list[PublicationEvidence] = []
    resident_obs: list[PublicationEvidence] = []
    recurring_topics: set[str] = set()

    for evi in evidence_items:
        if evi.publication_use == "EXCLUDE":
            continue
        if evi.kind in ("community_observation", "quote_assertion"):
            resident_obs.append(evi)
        else:
            general_facts.append(evi)

    for card in cards:
        if card.category:
            recurring_topics.add(card.category)
        for tag in card.tags:
            recurring_topics.add(tag)

    sorted_timeline = tuple(sorted(operational_observations, key=lambda x: x.observed_at))

    return ArticleEditorialContext(
        headline_candidates=tuple(headlines),
        operational_timeline=sorted_timeline,
        evidence_index=tuple(evidence_items),
        evidence_by_id=evidence_by_id,
        recurring_topics=tuple(sorted(recurring_topics)),
        general_facts=tuple(general_facts),
        resident_observations=tuple(resident_obs),
    )
