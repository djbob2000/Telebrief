"""Article-fidelity W6.3: the unspecified-district note is placed once per article."""

# ruff: noqa: S101
import datetime as dt
from pathlib import Path

import pytest

from src.city_context import CityContextResolver
from src.publication import article_quality as aq
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_finalization import prepare_article_draft
from src.publication.article_models import ArticleParagraph, ArticleSection, StructuredArticleDraft

PROFILE = Path(__file__).resolve().parents[1] / "data/city_profiles/berdyansk.yaml"
NOTE = "(район не уточняется)"
OBSERVED = dt.datetime(2026, 11, 11, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module")
def resolver():
    return aq._DiagnosisPlaceResolver(CityContextResolver.from_yaml(PROFILE))


def _support(support_id: str, text: str, *, story: str = "story:1") -> ArticleSupport:
    return ArticleSupport(
        support_id=support_id,
        text=text,
        source_text=text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=(f"telegram:source:1:item:{support_id}:rev:1:frag:1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=OBSERVED,
        evidence_kind="community_report",
        source_roles=("community",),
        story_id=story,
    )


def _context(*supports: ArticleSupport) -> ArticleEditorialContext:
    return ArticleEditorialContext(
        headline_candidates=(),
        support_index=tuple(supports),
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
        edition_slug="berdyansk",
    )


def _draft(
    *,
    title: str,
    lead: str,
    paragraphs: tuple[str, ...],
    support_ids: tuple[str, ...],
) -> StructuredArticleDraft:
    return StructuredArticleDraft(
        title=title,
        title_support_ids=support_ids[:1],
        lead=lead,
        lead_support_ids=support_ids,
        sections=(
            ArticleSection(
                heading="Свет и вода",
                paragraphs=tuple(
                    ArticleParagraph(text=text, cited_support_ids=support_ids)
                    for text in paragraphs
                ),
            ),
        ),
    )


def _prepare(draft: StructuredArticleDraft, context: ArticleEditorialContext, resolver):
    return prepare_article_draft(
        draft,
        context=context,
        material_projection=None,
        place_resolver=resolver,
    )


def test_note_is_placed_in_the_lead_only_when_the_lead_is_the_first_mention(resolver):
    context = _context(_support("s1", "В частном секторе нет света 20 дней."))
    draft = _draft(
        title="Бердянск: свет",
        lead="В частном секторе света нет уже 20 дней.",
        paragraphs=("В частном секторе нет и воды.",),
        support_ids=("s1",),
    )

    prepared = _prepare(draft, context, resolver)

    assert f"В частном секторе {NOTE} света" in prepared.lead
    paragraph = prepared.sections[0].paragraphs[0].text
    assert "район" not in paragraph.casefold()
    assert "В частном секторе нет и воды." == paragraph
    assert prepared.title == draft.title


def test_note_moves_to_the_first_paragraph_when_the_lead_has_no_mention(resolver):
    context = _context(_support("s1", "В частном секторе нет света 20 дней."))
    draft = _draft(
        title="Бердянск: свет",
        lead="В городе продолжаются перебои со светом.",
        paragraphs=("В частном секторе света нет 20 дней.", "В частном секторе нет воды."),
        support_ids=("s1",),
    )

    prepared = _prepare(draft, context, resolver)

    assert prepared.lead == draft.lead
    first, second = (paragraph.text for paragraph in prepared.sections[0].paragraphs)
    assert NOTE in first
    assert "район" not in second.casefold()


def test_note_is_not_repeated_in_any_later_unit(resolver):
    context = _context(_support("s1", "В частном секторе нет света 20 дней."))
    draft = _draft(
        title="Бердянск: свет",
        lead="В частном секторе света нет 20 дней.",
        paragraphs=("В частном секторе света нет 20 дней.",) * 3,
        support_ids=("s1",),
    )

    prepared = _prepare(draft, context, resolver)

    text = " ".join(
        (prepared.lead, *(paragraph.text for paragraph in prepared.sections[0].paragraphs))
    )
    assert text.count(NOTE) == 1


def test_specific_source_area_gets_no_note(resolver):
    """Faithful case: the source names the area, so no clarification is added."""
    context = _context(_support("s1", "На Горе, в частном секторе, света нет 20 дней."))
    draft = _draft(
        title="Бердянск: свет",
        lead="На Горе, в частном секторе, света нет 20 дней.",
        paragraphs=("В частном секторе света нет 20 дней.",),
        support_ids=("s1",),
    )

    prepared = _prepare(draft, context, resolver)

    assert prepared == draft


def test_title_never_gets_the_note(resolver):
    context = _context(_support("s1", "В частном секторе нет света 20 дней."))
    draft = _draft(
        title="Частный сектор без света",
        lead="Жители частного сектора без света.",
        paragraphs=("В частном секторе света нет 20 дней.",),
        support_ids=("s1",),
    )

    prepared = _prepare(draft, context, resolver)

    assert prepared.title == "Частный сектор без света"
    assert NOTE not in prepared.title
    # The lead is the first unit in reading order, so it carries the single note.
    assert prepared.lead.count(NOTE) == 1
    assert NOTE not in prepared.sections[0].paragraphs[0].text


def test_quoted_mention_gets_a_sentence_note_once(resolver):
    context = _context(_support("s1", "В частном секторе нет света 20 дней."))
    draft = _draft(
        title="Бердянск: свет",
        lead="Житель написал: «В частном секторе нет света».",
        paragraphs=("В частном секторе света нет 20 дней.",),
        support_ids=("s1",),
    )

    prepared = _prepare(draft, context, resolver)

    assert prepared.lead.endswith("Район не уточняется.")
    assert "«В частном секторе нет света»" in prepared.lead
    assert "район" not in prepared.sections[0].paragraphs[0].text.casefold()


def test_note_wording_does_not_expose_how_the_text_was_collected(resolver):
    context = _context(_support("s1", "В частном секторе нет света 20 дней."))
    draft = _draft(
        title="Бердянск: свет",
        lead="В частном секторе света нет 20 дней.",
        paragraphs=(),
        support_ids=("s1",),
    )

    prepared = _prepare(draft, context, resolver)

    assert "сообщени" not in prepared.lead.casefold()
    assert NOTE in prepared.lead


def test_validator_does_not_repeat_the_finding_after_the_note(resolver):
    """Defect: the note was in the lead and P003 was flagged again as unspecified."""
    with_note = _draft(
        title="Бердянск: свет",
        lead="В частном секторе " + NOTE + " света нет 20 дней.",
        paragraphs=("В частном секторе нет воды.",),
        support_ids=("s1",),
    )
    later = with_note.sections[0].paragraphs[0]
    assert aq._private_sector_note_precedes(with_note, later) is True

    without_note = _draft(
        title="Бердянск: свет",
        lead="В городе перебои со светом.",
        paragraphs=("В частном секторе нет воды.",),
        support_ids=("s1",),
    )
    assert (
        aq._private_sector_note_precedes(without_note, without_note.sections[0].paragraphs[0])
        is False
    )


def test_clarified_wording_satisfies_the_unclear_location_check():
    assert aq._has_unresolved_private_sector_location("В частном секторе света нет.")
    assert not aq._has_unresolved_private_sector_location(
        "В частном секторе " + NOTE + " света нет."
    )
    assert aq.UNSPECIFIED_DISTRICT_NOTE_RE.search("Район не уточняется.")
