"""Digest-only projection of fragments with their exact own-message context."""

from __future__ import annotations

import re
from dataclasses import replace

from src.editorial_models import EditorialAnalysis, StoryCard, StoryElement
from src.publication.evidence import PublicationEvidence

_ENROLLMENT_RE = re.compile(
    r"\b(?:набор|запис\w*|при[её]м\s+заявок)\b[^.!?\n]{0,70}"
    r"\b(?:дрессиров\w*|курс\w*|секци\w*|кружк\w*|обучен\w*)\b",
    re.IGNORECASE,
)
_CORRECTION_OFFER_RE = re.compile(
    r"коррекци\w*\s+поведени\w*\s+собак|собака\s+не\s+слушается",
    re.IGNORECASE,
)
_WATCH_AD_RE = re.compile(r"\bремонт\s+часов\b", re.IGNORECASE)
_WATCH_FRAGMENT_RE = re.compile(r"\b(?:время|режим)\s+работы\b", re.IGNORECASE)
_PENSION_AD_RE = re.compile(r"\bпомощь\s+с\s+пенсиями[^.!?\n]{0,65}банками\b", re.IGNORECASE)
_PENSION_FRAGMENT_RE = re.compile(
    r"\b(?:перевод\s+выплат|смена\s+банка\s+для\s+получения\s+пенсии)\b",
    re.IGNORECASE,
)
_PROMOTION_RE = re.compile(
    r"\b(?:контакты\s+для\s+связи|телефон|решаем\s+сложные\s+вопросы|"
    r"покупаем\s+коллекционные|честная\s+оценка)\b",
    re.IGNORECASE,
)
_SERVICE_UPDATE_RE = re.compile(
    r"\b(?:нет\s+(?:света|воды|газа)|не\s+работа\w*|не\s+принима\w*|"
    r"отключили|включили|перебои|прорыв|восстановили|отменили|перенесли|"
    r"изменили\s+график|открыл\w*\s+после\s+ремонта|"
    r"свет\s+появился|воду\s+(?:дали|дают|подают))\b",
    re.IGNORECASE,
)
_INSTITUTION_RE = re.compile(r"\b(?:клуб|школа|центр|ДОСААФ)\b", re.IGNORECASE)
_WEEKDAY_TIME_RE = re.compile(
    r"\b(?:пн|вт|ср|чт|пт|сб|вс|понедельник\w*|вторник\w*|сред\w*|"
    r"четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)\b.*\b\d{1,2}:\d{2}\b",
    re.IGNORECASE,
)
_PLACE_RE = re.compile(r"^(?:площадка|место\s+занятий|адрес\s+занятий)\b", re.IGNORECASE)


def _matching_text(text: str) -> str:
    return re.sub(r"[*_#]", "", text).lstrip(" \t📍🕒🐕🐾🍂")


def _own_context(evidence: PublicationEvidence) -> str:
    """Accept only the immutable own-item revision named by the fragment ref."""
    revision_id = evidence.source_item_revision_id
    if revision_id is None or not re.search(
        rf":item:{evidence.source_item_id}:rev:{revision_id}:frag:{evidence.fragment_id}$",
        evidence.source_ref,
    ):
        return ""
    return evidence.source_item_context_text


def _is_private_fragment(evidence: PublicationEvidence, context: str) -> bool:
    if not context:
        return False
    plain_context = _matching_text(context)
    fragment = _matching_text(evidence.source_text)
    # Actual service changes embedded alongside promotional copy remain usable.
    # Neither an inferred headline nor the surrounding ad can supply that change.
    if _SERVICE_UPDATE_RE.search(fragment):
        return False
    if _PROMOTION_RE.search(plain_context) and (
        (_WATCH_AD_RE.search(plain_context) and _WATCH_FRAGMENT_RE.search(fragment))
        or (_PENSION_AD_RE.search(plain_context) and _PENSION_FRAGMENT_RE.search(fragment))
    ):
        return True
    return bool(
        _CORRECTION_OFFER_RE.search(evidence.text)
        and _ENROLLMENT_RE.search(plain_context)
        and not _ENROLLMENT_RE.search(evidence.text)
    )


def _enrollment_details(context: str) -> str:
    """Copy a single announcement's concrete details, keeping source wording."""
    if len(_ENROLLMENT_RE.findall(_matching_text(context))) != 1:
        return ""
    lines = context.splitlines()
    selected: list[str] = []
    in_schedule = False
    for index, line in enumerate(lines):
        plain = _matching_text(line).strip()
        if not plain:
            in_schedule = False
            continue
        if index == 0 and _INSTITUTION_RE.search(plain):
            selected.append(line.strip())
        if re.fullmatch(r"расписание\s*:", plain, re.IGNORECASE):
            in_schedule = True
        elif in_schedule and _WEEKDAY_TIME_RE.search(plain):
            selected.append(line.strip())
        else:
            in_schedule = False
        if _PLACE_RE.search(plain) or re.match(r"занятия\s+до\b", plain, re.IGNORECASE):
            selected.append(line.strip())
    # An institution name on its own is context, not additional material.
    if len(selected) < 2:
        return ""
    return "\n".join(dict.fromkeys(selected))


def _aliases(evidence: PublicationEvidence) -> set[str]:
    return {evidence.evidence_id, evidence.source_ref, f"fragment:{evidence.fragment_id}"}


def _project_card(
    card: StoryCard,
    kept: list[PublicationEvidence],
    removed: list[PublicationEvidence],
    details: list[PublicationEvidence],
) -> StoryCard:
    safe_refs = set().union(*(_aliases(item) for item in kept))
    removed_texts = {item.text for item in removed}

    def elements(items: list[StoryElement]) -> list[StoryElement]:
        return [
            replace(item, source_refs=[ref for ref in item.source_refs if ref in safe_refs])
            for item in items
            if item.text not in removed_texts and any(ref in safe_refs for ref in item.source_refs)
        ]

    extra_details = [
        StoryElement(text=item.text, source_refs=[item.source_ref]) for item in details
    ]
    if not removed:
        return replace(
            card,
            summary=" ".join(dict.fromkeys(item.text for item in kept)),
            useful_details=[*card.useful_details, *extra_details],
        )
    return replace(
        card,
        topic=kept[0].text,
        summary=" ".join(dict.fromkeys(item.text for item in kept)),
        representative_source_refs=list(dict.fromkeys(item.source_ref for item in kept)),
        hard_facts=elements(card.hard_facts),
        community_observations=elements(card.community_observations),
        useful_details=[*elements(card.useful_details), *extra_details],
        uncertainties=[
            replace(
                item,
                related_source_refs=[ref for ref in item.related_source_refs if ref in safe_refs],
            )
            for item in card.uncertainties
            if any(ref in safe_refs for ref in item.related_source_refs)
        ],
        current_status="",
        next_known_step="",
        editorial_angle=None,
    )


def project_digest_source_material(analysis: EditorialAnalysis) -> EditorialAnalysis:
    """Project before admission; retain original source context and mixed Stories.

    This is writer material preparation, never deterministic publication rendering.
    It uses no reply-parent text, mutable source heads or corroboration threshold.
    """
    evidence = dict(analysis.evidence)
    changed = False
    removed_aliases: set[str] = set()
    cards: list[StoryCard] = []
    for card in analysis.cards:
        story_evidence = [
            item
            for item in analysis.evidence.values()
            if isinstance(item, PublicationEvidence)
            and f"story:{item.story_id}" == card.id
            and item.publication_use == "PUBLISH"
            and item.kind != "resident_question"
        ]
        if not story_evidence:
            cards.append(card)
            continue
        removed: list[PublicationEvidence] = []
        kept: list[PublicationEvidence] = []
        details: list[PublicationEvidence] = []
        context_seen: set[int] = set()
        for item in story_evidence:
            context = _own_context(item)
            if _is_private_fragment(item, context):
                removed.append(item)
                evidence[item.evidence_id] = replace(item, publication_use="CONTEXT")
                removed_aliases.update(_aliases(item))
                continue
            kept.append(item)
            revision_id = item.source_item_revision_id
            if (
                context
                and revision_id is not None
                and revision_id not in context_seen
                and _ENROLLMENT_RE.search(item.text)
            ):
                context_seen.add(revision_id)
                detail_text = _enrollment_details(context)
                if detail_text:
                    detail_id = f"{card.id}:source-item-context:{revision_id}"
                    if detail_id in analysis.evidence:
                        continue
                    detail = replace(
                        item,
                        evidence_id=detail_id,
                        text=detail_text,
                        source_text=context,
                        source_ref=item.source_ref.rsplit(":frag:", 1)[0] + ":item-context",
                        source_scope="source_item_revision",
                    )
                    evidence[detail_id] = detail
                    details.append(detail)
        if removed or details:
            changed = True
        if kept:
            cards.append(
                _project_card(card, kept, removed, details) if removed or details else card
            )
    if not changed:
        return analysis
    rollup = analysis.city_situation
    if rollup is not None and removed_aliases:
        kept_aliases = set().union(
            *(
                _aliases(item)
                for item in evidence.values()
                if isinstance(item, PublicationEvidence) and item.publication_use == "PUBLISH"
            )
        )
        rollup = replace(
            rollup,
            items=tuple(
                item
                for item in rollup.items
                if not (
                    set(item.current_source_refs or item.source_refs)
                    and set(item.current_source_refs or item.source_refs).issubset(
                        removed_aliases - kept_aliases
                    )
                )
            ),
        )
    return replace(analysis, cards=cards, evidence=evidence, city_situation=rollup)
